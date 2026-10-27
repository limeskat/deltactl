"""Command-line interface for the Delta DVP PLC driver.

Implements ``plc read``, ``plc write``, and ``plc watch``. The argument parser is assembled so
later commands (``doctor``, ``mock``) can extend it through the same
:func:`_add_common` helper without duplicating the global options.

Exit codes follow the CLI contract: ``0`` success, ``1`` operational failure,
``2`` usage. Every value is validated and encoded *before* a :class:`DeltaPLC`
is constructed, so a usage error never opens a socket.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import io
import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from typing import Any

from .addressing import Device, expand_tags, parse_raw, parse_tag, validate_span
from .client import DeltaPLC
from .errors import (
    AddressRangeError,
    AddressSyntaxError,
    BlockBoundaryError,
    DeltaPLCError,
    DeltaValueError,
    PollerError,
    ReadOnlyAddressError,
)
from .packing import DType, encode_values, mask_u16
from .planning import Tag, make_tag, plan_chunks
from .poller import CsvWriter, FrameWriter, NdjsonWriter, TableWriter, TagPoller
from .transport import AsyncDeltaPLC

__all__ = ["build_parser", "main"]

_DTYPES = ("u16", "i16", "i32", "u32", "f32")
_RAW_TYPES = ("d", "m", "x", "y")

# argparse's negative-number matcher accepts decimal only, so `-0x10` and
# friends are rewritten to decimal before parsing to keep them positional.
_NEG_BASE_RE = re.compile(r"^-0[xXbBoO][0-9a-fA-F]+$")


def _rewrite_negative_base(token: str) -> str:
    """Rewrite ``-0x10`` tokens to decimal so argparse keeps them positional."""
    if _NEG_BASE_RE.match(token):
        try:
            return str(int(token, 0))
        except ValueError:
            return token
    return token

# Pre-I/O validation errors that are the caller's fault and exit 2.
_USAGE_ERRORS = (
    AddressSyntaxError,
    AddressRangeError,
    BlockBoundaryError,
    DeltaValueError,
    PollerError,
)


# -- parser ----------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    """Add the global options shared by the root parser and every subcommand.

    ``default=argparse.SUPPRESS`` keeps a flag given before the subcommand from
    being overwritten by the subparser's default (subparser results are copied
    onto the namespace, so a suppressed default simply disappears).
    """
    group = parser.add_argument_group("global options")
    group.add_argument("--host", metavar="HOST", default=argparse.SUPPRESS)
    group.add_argument("--port", type=int, metavar="PORT", default=argparse.SUPPRESS)
    group.add_argument("--unit", type=int, metavar="ID", default=argparse.SUPPRESS)
    group.add_argument(
        "--timeout", type=float, metavar="SECONDS", default=argparse.SUPPRESS
    )
    group.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    group.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS)
    group.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS)


def _add_read_options(parser: argparse.ArgumentParser) -> None:
    """Add the ``read``-specific positionals and options."""
    parser.add_argument("tags", nargs="*", metavar="TAG")
    parser.add_argument("--count", type=int, metavar="N", default=argparse.SUPPRESS)
    parser.add_argument(
        "--dtype", type=str.lower, choices=_DTYPES, default=argparse.SUPPRESS
    )
    parser.add_argument("--raw", metavar="ADDR", default=argparse.SUPPRESS)
    parser.add_argument(
        "--raw-type",
        type=str.lower,
        choices=_RAW_TYPES,
        default=argparse.SUPPRESS,
    )


def _add_write_options(parser: argparse.ArgumentParser) -> None:
    """Add the ``write``-specific positionals and options."""
    parser.add_argument("args", nargs="+", metavar="ARG")
    parser.add_argument(
        "--dtype", type=str.lower, choices=_DTYPES, default=argparse.SUPPRESS
    )
    parser.add_argument("--raw", metavar="ADDR", default=argparse.SUPPRESS)
    parser.add_argument(
        "--raw-type",
        type=str.lower,
        choices=_RAW_TYPES,
        default=argparse.SUPPRESS,
    )
    parser.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)


def _add_watch_options(parser: argparse.ArgumentParser) -> None:
    """Add the ``watch``-specific positionals and options."""
    parser.add_argument("tags", nargs="+", metavar="TAG")
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        metavar="SECONDS",
    )
    parser.add_argument(
        "--on-change",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--deadband",
        type=float,
        default=0.0,
        metavar="FLOAT",
    )
    parser.add_argument(
        "--heartbeat",
        type=float,
        default=10.0,
        metavar="SECONDS",
    )
    parser.add_argument(
        "--format",
        type=str.lower,
        choices=("ndjson", "csv", "table"),
        default=None,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="SECONDS",
    )
    group.add_argument(
        "--count",
        type=int,
        default=None,
        metavar="N",
    )
    parser.add_argument(
        "--dtype",
        type=str.lower,
        choices=_DTYPES,
        default=argparse.SUPPRESS,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the ``plc`` parser containing the ``read``, ``write``, and ``watch`` commands."""
    parser = argparse.ArgumentParser(
        prog="plc", description="Delta DVP PLC driver over Modbus TCP."
    )
    _add_common(parser)
    parser.set_defaults(_parser=parser)
    subparsers = parser.add_subparsers(
        dest="command", metavar="COMMAND", required=True
    )

    read = subparsers.add_parser("read", help="read one or more tags")
    _add_common(read)
    _add_read_options(read)
    read.set_defaults(_parser=read)

    write = subparsers.add_parser("write", help="write one or more values to a tag")
    _add_common(write)
    _add_write_options(write)
    write.set_defaults(_parser=write)

    watch = subparsers.add_parser(
        "watch", help="poll tags on an interval and stream frames"
    )
    _add_common(watch)
    _add_watch_options(watch)
    watch.set_defaults(_parser=watch)

    return parser


# -- entry point -----------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    tokens = list(sys.argv[1:] if argv is None else argv)
    tokens = [_rewrite_negative_base(t) for t in tokens]
    parser = build_parser()
    args = parser.parse_args(tokens)
    _configure_logging(args)
    try:
        if args.command == "read":
            return _cmd_read(args)
        if args.command == "watch":
            return _cmd_watch(args)
        return _cmd_write(args)
    except _USAGE_ERRORS as exc:
        return _fail(exc, 2)
    except ReadOnlyAddressError as exc:
        return _fail(exc, 1)
    except DeltaPLCError as exc:
        return _fail(exc, 1)
    except OSError as exc:
        return _fail(exc, 1)
    except ValueError as exc:
        # `expand_tags`, `int(...)`, `float(...)`, and malformed env values.
        return _fail(exc, 2)
    except KeyboardInterrupt:
        return 130


def _fail(exc: object, code: int) -> int:
    """Print ``error: <exc>`` to stderr and return *code* (data stays on stdout)."""
    print(f"error: {exc}", file=sys.stderr)
    return code


def _configure_logging(args: argparse.Namespace) -> None:
    """Raise (``-v``, repeatable) or lower (``-q``) the log level on stderr."""
    verbose = getattr(args, "verbose", 0) or 0
    quiet = bool(getattr(args, "quiet", False))
    if not verbose and not quiet:
        return
    if quiet:
        level = logging.ERROR
    elif verbose >= 2:
        level = logging.DEBUG
    elif verbose >= 1:
        level = logging.INFO
    else:  # pragma: no cover - defensive; unreachable given the guard above
        level = logging.WARNING
    logging.basicConfig(level=level, stream=sys.stderr, force=True)


# -- global option resolution ---------------------------------------------


def _common_settings(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> tuple[str, int, int, float]:
    """Resolve host/port/unit/timeout with flag > environment > default."""
    host = getattr(args, "host", None)
    if host is None:
        host = os.environ.get("DELTA_PLC_HOST")
    if not host:
        parser.error("--host is required (or set DELTA_PLC_HOST)")
    port = _resolve_int(args, "port", "DELTA_PLC_PORT", 502)
    unit = _resolve_int(args, "unit", "DELTA_PLC_UNIT", 1)
    timeout = _resolve_float(args, "timeout", "DELTA_PLC_TIMEOUT", 2.0)
    return host, port, unit, timeout


def _resolve_int(
    args: argparse.Namespace, attr: str, env: str, default: int
) -> int:
    """Flag value, else ``env`` parsed as decimal, else *default*."""
    value = getattr(args, attr, None)
    if value is not None:
        return value
    raw = os.environ.get(env)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise DeltaValueError(f"invalid {env}={raw!r}: expected an integer") from None


def _resolve_float(
    args: argparse.Namespace, attr: str, env: str, default: float
) -> float:
    """Flag value, else ``env`` parsed as a float, else *default*."""
    value = getattr(args, attr, None)
    if value is not None:
        return value
    raw = os.environ.get(env)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise DeltaValueError(f"invalid {env}={raw!r}: expected a number") from None


def _check_dtype_target(
    text: str, dtype: str | None, parser: argparse.ArgumentParser
) -> None:
    """Reject ``--dtype`` unless *text* is a plain (non-bit) D tag."""
    if dtype is None:
        return
    parsed = parse_tag(text)
    if parsed.address.device is not Device.D or parsed.address.bit is not None:
        parser.error(f"--dtype is only valid for D word tags, not {parsed.display}")


# -- read ------------------------------------------------------------------


def _cmd_read(args: argparse.Namespace) -> int:
    """Implement ``plc read``."""
    parser: argparse.ArgumentParser = args._parser
    raw = getattr(args, "raw", None)
    raw_type = getattr(args, "raw_type", None)
    dtype = getattr(args, "dtype", None)
    count = getattr(args, "count", None)
    tags = list(args.tags)

    if count is not None and count < 1:
        parser.error("--count must be at least 1")
    if raw is not None and tags:
        parser.error("--raw cannot be combined with positional tags")
    if raw is None and raw_type is not None:
        parser.error("--raw-type requires --raw")
    if raw is not None and dtype is not None and raw_type not in (None, "d"):
        parser.error("--dtype is only valid with --raw-type d")

    if raw is not None:
        tag_texts = [parse_raw(raw, raw_type or "d").display]
    else:
        if not tags:
            parser.error("read requires at least one tag")
        tag_texts = expand_tags(tags)
        if count is not None and len(tag_texts) > 1:
            parser.error("--count requires exactly one tag or raw address")

    for text in tag_texts:
        _check_dtype_target(text, dtype, parser)
    default_dtype = DType(dtype) if dtype is not None else None
    resolved = [make_tag(text, default_dtype) for text in tag_texts]

    single = len(resolved) == 1
    value_count = (count or 1) if single else 1
    for tag in resolved:
        if tag.dtype is DType.BIT and tag.address.device is Device.D and value_count > 1:
            raise DeltaValueError(
                f"{tag.name} is a D word bit; --count is not supported"
            )
        plan_chunks(tag.address, tag.width, value_count)

    host, port, unit, timeout = _common_settings(args, parser)
    use_json = bool(getattr(args, "json", False))

    with DeltaPLC(host, port=port, unit=unit, timeout=timeout) as plc:
        if single:
            values = _read_one(plc, resolved[0], value_count)
            results = [(resolved[0].name, values)]
        else:
            results = _read_many(plc, resolved)

    _emit_read(results, host, port, unit, use_json)
    return 0


def _read_one(
    plc: DeltaPLC, tag: Tag, count: int
) -> list[int | float | bool]:
    """Read *count* values for one resolved tag, always as a list."""
    if tag.dtype is DType.BIT:
        if tag.address.device is Device.D:
            return [bool(plc.read(tag.name))]
        bits = plc.read_bits(tag.name, count)
        res_bits: list[int | float | bool] = list(bits) if isinstance(bits, list) else [bits]
        return res_bits
    values = plc.read(_read_name(tag), count)
    res_vals: list[int | float | bool] = list(values) if isinstance(values, list) else [values]
    return res_vals


def _read_many(plc: DeltaPLC, tags: Sequence[Tag]) -> list[tuple[str, Any]]:
    """Read several tags in one coalesced pass, keyed by canonical display name."""
    names = [_read_name(tag) for tag in tags]
    values = plc.read_many(names)
    return [(tag.name, [values[name]]) for tag, name in zip(tags, names)]


def _read_name(tag: Tag) -> str:
    """Tag text for the driver: keeps an inline suffix, adds a resolved one."""
    if ":" in tag.name:
        return tag.name
    if tag.dtype in (DType.I16, DType.I32, DType.U32, DType.F32):
        return f"{tag.name}:{tag.dtype.value}"
    return tag.name


def _emit_read(
    results: Sequence[tuple[str, Any]], host: str, port: int, unit: int, use_json: bool
) -> None:
    """Print a read result as JSON or one ``NAME = value`` line per value."""
    if use_json:
        payload = {
            "host": host,
            "port": port,
            "unit": unit,
            "ts": _timestamp(),
            "values": {
                name: (values[0] if len(values) == 1 else list(values))
                for name, values in results
            },
        }
        print(json.dumps(payload))
        return
    for name, values in results:
        if len(values) == 1:
            print(f"{name} = {_format_value(values[0])}")
        else:
            for index, value in enumerate(values):
                print(f"{name}[{index}] = {_format_value(value)}")


# -- write -----------------------------------------------------------------


def _cmd_write(args: argparse.Namespace) -> int:
    """Implement ``plc write``."""
    parser: argparse.ArgumentParser = args._parser
    raw = getattr(args, "raw", None)
    raw_type = getattr(args, "raw_type", None)
    dtype = getattr(args, "dtype", None)
    dry_run = bool(getattr(args, "dry_run", False))
    items = list(args.args)

    if raw is None and raw_type is not None:
        parser.error("--raw-type requires --raw")
    if raw is not None and dtype is not None and raw_type not in (None, "d"):
        parser.error("--dtype is only valid with --raw-type d")

    if raw is not None:
        tag_text = parse_raw(raw, raw_type or "d").display
        value_texts = items
    else:
        tag_text = items[0]
        value_texts = items[1:]
        if not value_texts:
            parser.error("write requires at least one value")

    _check_dtype_target(tag_text, dtype, parser)
    default_dtype = DType(dtype) if dtype is not None else None
    tag = make_tag(tag_text, default_dtype)
    explicit = dtype is not None or ":" in tag_text
    count = len(value_texts)

    if tag.dtype is DType.BIT and tag.address.device is Device.D and count > 1:
        raise DeltaValueError(f"{tag.name} is a D word bit: exactly one value is required")

    total = count * tag.width if tag.address.device is Device.D else count
    validate_span(tag.address, total, write=True, allow_block_crossing=True)

    values = [_parse_value(text, tag) for text in value_texts]
    encoded, display = _encode_write_values(tag, values, explicit)
    frames = _plan_frames(tag, encoded, count)

    if dry_run:
        _emit_dry_run(frames, bool(getattr(args, "json", False)))
        return 0

    host, port, unit, timeout = _common_settings(args, parser)
    with DeltaPLC(host, port=port, unit=unit, timeout=timeout) as plc:
        _perform_write(plc, tag, values, explicit)

    _emit_write(tag.name, display, bool(getattr(args, "json", False)))
    return 0


def _parse_value(text: str, tag: Tag) -> int | float | bool:
    """Parse one positional value for *tag*'s dtype."""
    if tag.dtype is DType.BIT:
        lowered = text.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
        raise DeltaValueError(f"invalid bit value {text!r}: expected true/false/1/0")
    if tag.dtype is DType.F32:
        return float(text)
    return int(text, 0)


def _encode_write_values(
    tag: Tag, values: Sequence[int | float | bool], explicit: bool
) -> tuple[list[Any], list[Any]]:
    """Return ``(registers, display_values)`` for a pre-validated write.

    Untyped plain-D writes mask through :func:`mask_u16` (the driver's untyped
    semantics); an explicit dtype encodes so its typed range applies.
    """
    if tag.dtype is DType.BIT:
        bits = [bool(value) for value in values]
        return bits, bits
    if explicit:
        return encode_values(values, tag.dtype), list(values)
    masked: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise DeltaValueError(f"{value!r} is not an integer for a D word")
        masked.append(mask_u16(value))
    return masked, masked


def _perform_write(
    plc: DeltaPLC, tag: Tag, values: Sequence[int | float | bool], explicit: bool
) -> None:
    """Send a pre-validated write through the appropriate client method."""
    payload: Any = values[0] if len(values) == 1 else list(values)
    if tag.dtype is DType.BIT:
        if tag.address.device is Device.D:
            plc.write(tag.name, values[0])
        else:
            plc.write_bits(tag.name, payload)
        return
    if explicit:
        method = {
            DType.U16: plc.write_u16,
            DType.I16: plc.write_i16,
            DType.I32: plc.write_i32,
            DType.U32: plc.write_u32,
            DType.F32: plc.write_f32,
        }[tag.dtype]
        method(tag.name, payload)
        return
    plc.write(tag.name, payload)


def _plan_frames(tag: Tag, encoded: Sequence[Any], count: int) -> list[dict[str, Any]]:
    """Build the dry-run frame list for a pre-validated, pre-encoded write."""
    if tag.dtype is DType.BIT and tag.address.device is Device.D:
        # A word-bit write is a read-modify-write in the driver; show the write leg.
        return [_frame(6, tag.address.wire, 1, [int(bool(encoded[0]))])]
    frames: list[dict[str, Any]] = []
    for chunk in plan_chunks(tag.address, tag.width, count):
        if tag.dtype is DType.BIT:
            function = 5 if chunk.values == 1 else 15
            values = encoded[chunk.first : chunk.first + chunk.values]
        else:
            function = 6 if chunk.values == 1 and tag.width == 1 else 16
            values = encoded[chunk.first * tag.width : (chunk.first + chunk.values) * tag.width]
        frames.append(_frame(function, chunk.start, chunk.count, values))
    return frames


def _frame(
    function: int, wire: int, count: int, values: Sequence[Any]
) -> dict[str, Any]:
    """One dry-run frame: function code, wire address, count, encoded values."""
    return {
        "fc": function,
        "wire": f"0x{wire:04x}",
        "count": count,
        "values": [int(value) if isinstance(value, bool) else value for value in values],
    }


def _emit_dry_run(frames: Sequence[dict[str, Any]], use_json: bool) -> None:
    """Print dry-run frames; no I/O and exit 0."""
    if use_json:
        print(json.dumps({"dry_run": True, "frames": list(frames)}))
        return
    for frame in frames:
        print(
            f"FC{frame['fc']} wire={frame['wire']} "
            f"count={frame['count']} values={frame['values']}"
        )


def _emit_write(name: str, values: Sequence[Any], use_json: bool) -> None:
    """Print a successful write as JSON or ``NAME = value`` line(s)."""
    if use_json:
        payload: Any = values[0] if len(values) == 1 else list(values)
        print(json.dumps({"wrote": {name: payload}}))
        return
    if len(values) == 1:
        print(f"{name} = {_format_value(values[0])}")
    else:
        for index, value in enumerate(values):
            print(f"{name}[{index}] = {_format_value(value)}")


# -- watch -----------------------------------------------------------------


async def _watch(
    plc: AsyncDeltaPLC,
    tags: Sequence[Tag],
    args: argparse.Namespace,
    writer: FrameWriter,
) -> int:
    """Poll *tags* via :class:`TagPoller` until stopped or finished."""
    poller = TagPoller(
        plc,
        tags,
        interval=args.interval,
        on_change=args.on_change,
        deadband=args.deadband,
        heartbeat=args.heartbeat,
    )
    return await poller.run(
        duration=args.duration, count=args.count, writer=writer
    )


async def _run_watch(
    host: str,
    port: int,
    unit: int,
    timeout: float,
    tags: Sequence[Tag],
    args: argparse.Namespace,
    writer: FrameWriter,
) -> int:
    """Run the watch loop inside an :class:`AsyncDeltaPLC` context."""
    async with AsyncDeltaPLC(host, port=port, unit=unit, timeout=timeout) as plc:
        return await _watch(plc, tags, args, writer)


def _cmd_watch(args: argparse.Namespace) -> int:
    """Implement ``plc watch``."""
    parser: argparse.ArgumentParser = args._parser
    use_json = bool(getattr(args, "json", False))
    fmt = getattr(args, "format", None)
    if fmt is not None and fmt != "ndjson" and use_json:
        parser.error(f"--json cannot be combined with --format {fmt}")
    if fmt is None:
        fmt = "ndjson"

    duration = getattr(args, "duration", None)
    count = getattr(args, "count", None)
    if count is not None and count < 1:
        parser.error("--count must be at least 1")
    if duration is not None and duration < 0:
        parser.error("--duration must be non-negative")
    if args.interval < 0.05:
        parser.error(f"--interval must be at least 0.05, got {args.interval}")
    if args.deadband < 0:
        parser.error("--deadband must be non-negative")
    if args.heartbeat < 0:
        parser.error("--heartbeat must be non-negative")

    dtype = getattr(args, "dtype", None)
    tag_texts = expand_tags(args.tags)
    for text in tag_texts:
        _check_dtype_target(text, dtype, parser)
    default_dtype = DType(dtype) if dtype is not None else None
    resolved = [make_tag(text, default_dtype) for text in tag_texts]

    seen_names: set[str] = set()
    tags: list[Tag] = []
    for tag in resolved:
        if tag.name not in seen_names:
            seen_names.add(tag.name)
            tags.append(tag)

    names = [tag.name for tag in tags]
    writer: FrameWriter
    if fmt == "ndjson":
        writer = NdjsonWriter(sys.stdout)
    elif fmt == "csv":
        if hasattr(sys.stdout, "reconfigure"):
            with contextlib.suppress(AttributeError, io.UnsupportedOperation):
                sys.stdout.reconfigure(newline="")
        writer = CsvWriter(sys.stdout, names)
    else:
        writer = TableWriter(sys.stdout, names)

    host, port, unit, timeout = _common_settings(args, parser)
    try:
        asyncio.run(_run_watch(host, port, unit, timeout, tags, args, writer))
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        with contextlib.suppress(OSError, ValueError):
            writer.flush()
            writer.close()
        if hasattr(sys.stdout, "reconfigure"):
            with contextlib.suppress(AttributeError, io.UnsupportedOperation):
                sys.stdout.reconfigure(newline=None)


# -- shared output helpers -------------------------------------------------


def _format_value(value: Any) -> str:
    """Human formatting: booleans as lowercase ``true``/``false``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _timestamp() -> str:
    """Local timezone-aware ISO-8601 timestamp with milliseconds."""
    return datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")


if __name__ == "__main__":  # pragma: no cover - console-script convenience
    raise SystemExit(main())
