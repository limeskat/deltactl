"""Polling loop: frames, quality classification, and reconnect backoff.

:class:`TagPoller` turns a set of tags into a fixed read plan and runs it once
per interval. Every cycle emits at most one :class:`Frame`: ``GOOD`` when every
read group succeeded, ``DEGRADED`` when some failed (the frame carries only the
values that arrived), and ``BAD`` when all failed or the client is
disconnected.

While disconnected the poller emits a ``BAD`` frame per cycle and retries
``connect()`` on an exponential backoff (1, 2, 4, 8, 15 s, capped and jittered
by ``0.8 + 0.4 * rand()``). The clock, sleep, and random source are injectable
so tests run deterministically, and ``_poll_once`` is the single-cycle seam
used both by :meth:`TagPoller.run` and by filter tests.

Filtering happens inside ``_poll_once``: ``on_change`` emits only values that
moved past their deadband, a quality change or heartbeat promotes a cycle to a
full snapshot, and ``BAD`` bypasses filtering entirely. The writers at the top
of this module serialize frames as NDJSON, CSV, or an ASCII table for the CLI.
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import IO, Any, Protocol

from .addressing import SeriesProfile, validate_span
from .errors import DeltaPLCError, PollerError
from .planning import ReadGroup, Tag, make_tag, plan_reads
from .transport import GroupResult, decode_group_values

__all__ = [
    "CsvWriter",
    "Frame",
    "FrameWriter",
    "NdjsonWriter",
    "TableWriter",
    "TagPoller",
]

#: Reconnect delays in seconds; the last value repeats. Each one is scaled by
#: ``0.8 + 0.4 * rand()`` so a fleet of pollers does not reconnect in lockstep.
_BACKOFF_STEPS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 15.0)

#: Smallest poll interval the poller accepts, in seconds.
_MIN_INTERVAL: float = 0.05


@dataclass(frozen=True)
class Frame:
    """One poll result: when, which cycle, how fast, how good, and what values.

    ``error`` is set whenever the cycle did not fully succeed, and ``heartbeat``
    marks a full snapshot emitted only to prove liveness. ``values`` holds just
    the tags that decoded, in the poller's requested tag order.
    """

    ts: str
    seq: int
    rtt_ms: float
    quality: str
    values: dict[str, int | float | bool]
    error: str | None = None
    heartbeat: bool = False

    def to_dict(self) -> dict[str, object]:
        """Return the wire form: ``error`` only when set, ``heartbeat`` only when true."""
        payload: dict[str, object] = {
            "ts": self.ts,
            "seq": self.seq,
            "rtt_ms": self.rtt_ms,
            "quality": self.quality,
            "values": dict(self.values),
        }
        if self.error is not None:
            payload["error"] = self.error
        if self.heartbeat:
            payload["heartbeat"] = True
        return payload


class FrameWriter(Protocol):
    """Sink for emitted frames; ``flush``/``close`` are owned by the caller."""

    def write(self, frame: Frame) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


def _sanitize(value: Any) -> Any:
    """Recursively replace non-finite floats with ``None`` for strict JSON.

    ``Frame`` values may carry ``nan``/``±inf``; NDJSON must stay valid JSON,
    so every float is checked and containers are rebuilt along the way.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return value


class NdjsonWriter:
    """Write one compact JSON object per frame, newline-delimited.

    Non-finite floats are sanitized to ``null`` before a strict ``json.dumps``
    (``allow_nan=False``) so every line is valid JSON. Each frame is flushed.
    """

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def write(self, frame: Frame) -> None:
        """Serialize *frame* as one sanitized JSON line and flush."""
        payload = _sanitize(frame.to_dict())
        self._stream.write(json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n")
        self.flush()

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        """Flush only: the caller owns the underlying stream's lifetime."""
        self.flush()


class CsvWriter:
    """Write frames as CSV: a fixed schema followed by the requested tags.

    The header is written once, before the first row. Values are formatted for
    humans and spreadsheets: bools as ``true``/``false``, finite floats via
    ``repr``, non-finite floats as ``nan``/``inf``, ints via ``str``, and
    missing tags as an empty field. Everything is emitted through ``csv.writer``
    so the ``error`` text is quoted correctly.
    """

    _BASE: tuple[str, ...] = ("ts", "seq", "rtt_ms", "quality", "error")

    def __init__(self, stream: IO[str], tags: Sequence[str]) -> None:
        self._stream = stream
        self._tags = tuple(tags)
        self._writer = csv.writer(stream)
        self._header_written = False

    def write(self, frame: Frame) -> None:
        """Write the header once, then one row for *frame*."""
        if not self._header_written:
            self._writer.writerow([*self._BASE, *self._tags])
            self._header_written = True
        row: list[object] = [
            frame.ts,
            frame.seq,
            _format_scalar(frame.rtt_ms),
            frame.quality,
            frame.error if frame.error is not None else "",
        ]
        row.extend(_format_scalar(frame.values.get(name)) for name in self._tags)
        self._writer.writerow(row)

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        """Flush only: the caller owns the underlying stream's lifetime."""
        self.flush()


def _format_scalar(value: float | bool | None) -> str:
    """Render one value as CSV/table text; ``None`` (missing) becomes ``""``."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value) if math.isfinite(value) else str(value)
    return str(value)


class TableWriter:
    """Write frames as a fixed-width ASCII table, one row per frame.

    Missing values render as ``-``. The table contains no ANSI escapes so it is
    safe to pipe or redirect.
    """

    _BASE: tuple[str, ...] = ("ts", "seq", "rtt_ms", "quality", "error")

    def __init__(self, stream: IO[str], tags: Sequence[str]) -> None:
        self._stream = stream
        self._tags = tuple(tags)
        self._columns = (*self._BASE, *self._tags)
        self._widths = tuple(max(len(name), 6) for name in self._columns)
        self._header_written = False

    def write(self, frame: Frame) -> None:
        """Write the header once, then one ASCII row for *frame*."""
        if not self._header_written:
            self._stream.write(self._line(self._columns))
            self._stream.write(self._line(tuple("-" * width for width in self._widths)))
            self._header_written = True
        cells = [
            frame.ts,
            str(frame.seq),
            _format_scalar(frame.rtt_ms),
            frame.quality,
            frame.error if frame.error is not None else "-",
        ]
        cells.extend(_format_scalar(frame.values.get(name)) or "-" for name in self._tags)
        self._stream.write(self._line(cells))

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        """Flush only: the caller owns the underlying stream's lifetime."""
        self.flush()

    def _line(self, cells: Sequence[str]) -> str:
        """Left-justify *cells* into the fixed columns and end the line."""
        return (
            "  ".join(cell.ljust(width) for cell, width in zip(cells, self._widths)).rstrip() + "\n"
        )


class _PollClient(Protocol):
    """The slice of the async client the poller depends on."""

    @property
    def is_connected(self) -> bool: ...

    @property
    def effective_profile(self) -> SeriesProfile: ...

    async def connect(self) -> None: ...

    async def read_planned(self, groups: Sequence[ReadGroup]) -> list[GroupResult]: ...


def _error_text(exc: BaseException | None) -> str:
    """Human-readable *exc* text, falling back to its class name."""
    if exc is None:
        return "unknown error"
    return str(exc) or type(exc).__name__


class TagPoller:
    """Poll a fixed set of tags into frames, with quality and reconnect policy.

    Tags are parsed and planned once in ``__init__``, so every cycle reuses the
    same groups. ``on_change``, ``deadband``, and ``heartbeat`` drive the emit
    filter: with ``on_change`` the poller sends only values that moved past
    their per-tag (or global) deadband, while the first cycle, a quality change,
    a due heartbeat, and any ``BAD`` cycle send every available value.
    """

    def __init__(
        self,
        client: _PollClient,
        tags: Sequence[str | Tag],
        *,
        interval: float = 0.5,
        on_change: bool = False,
        deadband: float = 0.0,
        heartbeat: float = 10.0,
        max_gap: int = 10,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        if interval < _MIN_INTERVAL:
            raise PollerError(f"interval must be at least {_MIN_INTERVAL}s, got {interval}")
        if not tags:
            raise PollerError("TagPoller requires at least one tag")
        self._client = client
        profile = getattr(client, "effective_profile", None)
        resolved_tags: list[Tag] = []
        for item in tags:
            if isinstance(item, Tag):
                if profile is not None:
                    validate_span(item.address, item.width, write=False, profile=profile)
                resolved_tags.append(item)
            else:
                resolved_tags.append(make_tag(item, profile=profile))
        self._tags: tuple[Tag, ...] = tuple(resolved_tags)
        self._max_gap = max_gap
        # Planned once: a fixed tag set always maps to the same read groups.
        self._groups: list[ReadGroup] = plan_reads(self._tags, max_gap=max_gap)
        self._interval = interval
        self._on_change = on_change
        self._deadband = deadband
        self._heartbeat = heartbeat
        self._clock = clock
        self._sleep = sleep
        self._rand = rand
        self._seq = 0
        self._stopped = False
        # Reconnect state: ``-inf`` means the first disconnected cycle connects.
        self._backoff_step = 0
        self._next_attempt_at = float("-inf")
        self._last_error: str | None = None
        # Filter state: per-tag deadbands and the last *emitted* values/quality.
        self._deadbands: dict[str, float | None] = {tag.name: tag.deadband for tag in self._tags}
        self._last_emitted: dict[str, int | float | bool] = {}
        self._last_quality: str | None = None
        self._last_emit_time: float | None = None

    # -- lifecycle ---------------------------------------------------------

    async def run(
        self,
        *,
        duration: float | None = None,
        count: int | None = None,
        writer: FrameWriter | None = None,
    ) -> int:
        """Poll until *duration* elapses or *count* frames are emitted.

        At most one of *duration*/*count* may be set. The elapsed check runs
        before the first cycle, so ``duration=0`` emits nothing. Returns the
        number of frames emitted. Writer lifecycle (``flush``/``close``) belongs
        to the caller.
        """
        if duration is not None and count is not None:
            raise PollerError("run accepts duration or count, not both")
        start = self._clock()
        emitted = 0
        while not self._stopped:
            if duration is not None and self._clock() - start >= duration:
                break
            if count is not None and emitted >= count:
                break
            cycle_start = self._clock()
            emitted += await self._poll_once(cycle_start, writer)
            if (count is not None and emitted >= count) or (
                duration is not None and self._clock() - start >= duration
            ):
                break
            elapsed = self._clock() - cycle_start
            await self._sleep(max(0.0, self._interval - elapsed))
        return emitted

    def stop(self) -> None:
        """Ask the loop to exit after the current cycle; idempotent."""
        self._stopped = True

    # -- one cycle ---------------------------------------------------------

    async def _poll_once(self, now: float, writer: FrameWriter | None) -> int:
        """Run one poll cycle, apply the emit filters, and return frames emitted.

        *now* drives both reconnect scheduling and heartbeat timing so tests can
        advance a fake clock. A frame is emitted when it is the first cycle, the
        quality changed, a heartbeat is due, ``on_change`` is off, or some value
        changed beyond its deadband; ``BAD`` frames bypass filtering. The writer
        is flushed after each emitted frame.
        """
        if self._client.is_connected:
            quality, values, rtt_ms, error = await self._read_cycle()
        else:
            error = await self._reconnect(now)
            quality, values, rtt_ms = "BAD", {}, 0.0
        selected = self._select(now, quality, values)
        if selected is None:
            return 0
        emitted, heartbeat = selected
        # ``seq`` counts emitted frames only, so suppressed cycles do not
        # leave gaps in the stream consumers see.
        self._seq += 1
        frame = Frame(
            ts=datetime.now().astimezone().isoformat(timespec="milliseconds"),
            seq=self._seq,
            rtt_ms=rtt_ms,
            quality=quality,
            values=emitted,
            error=error,
            heartbeat=heartbeat,
        )
        self._last_emit_time = now
        self._last_emitted.update(emitted)
        self._last_quality = quality
        if writer is not None:
            writer.write(frame)
            writer.flush()
        return 1

    def _select(
        self,
        now: float,
        quality: str,
        values: dict[str, int | float | bool],
    ) -> tuple[dict[str, int | float | bool], bool] | None:
        """Choose what to emit for this cycle, or ``None`` to suppress it.

        Returns the values to send plus whether this is a heartbeat snapshot.
        ``BAD`` (disconnected or every group failed) bypasses filtering, as does
        the first cycle and any quality change, so consumers always see the
        current state when it materially changed.
        """
        heartbeat_due = self._heartbeat_due(now)
        if quality == "BAD":
            return dict(values), heartbeat_due
        if self._last_quality is None or quality != self._last_quality:
            return dict(values), heartbeat_due
        if heartbeat_due:
            return dict(values), True
        if not self._on_change:
            return dict(values), False
        changed = {name: value for name, value in values.items() if self._changed(name, value)}
        if not changed:
            return None
        return changed, False

    def _heartbeat_due(self, now: float) -> bool:
        """True when a liveness snapshot is due; ``heartbeat=0`` disables it."""
        if self._heartbeat <= 0 or self._last_emit_time is None:
            return False
        return now - self._last_emit_time >= self._heartbeat

    def _threshold(self, name: str) -> float:
        """Deadband for *name*: its tag override, else the poller default."""
        deadband = self._deadbands.get(name)
        return self._deadband if deadband is None else deadband

    def _changed(self, name: str, value: float | bool) -> bool:
        """True when *value* differs from the last **emitted** value of *name*.

        Bools change on strict inequality; numbers must move at least their
        deadband, and a zero delta is never a change even at deadband ``0``.
        A tag with no emitted history counts as changed.
        """
        if name not in self._last_emitted:
            return True
        last = self._last_emitted[name]
        if isinstance(value, bool) or isinstance(last, bool):
            return value != last
        if value == last:
            return False
        return abs(value - last) >= self._threshold(name)

    async def _read_cycle(
        self,
    ) -> tuple[str, dict[str, int | float | bool], float, str | None]:
        """Read the fixed plan and classify the cycle's quality."""
        try:
            results = await self._client.read_planned(self._groups)
        except (DeltaPLCError, OSError) as exc:
            # Defensive: group failures are reported in ``results``, but the
            # socket can drop between the is_connected check and the call.
            return "BAD", {}, 0.0, _error_text(exc)
        failed = [result for result in results if result.error is not None]
        if not failed:
            quality = "GOOD"
        elif len(failed) == len(results):
            quality = "BAD"
        else:
            quality = "DEGRADED"
        decoded: dict[str, int | float | bool] = {}
        for result in results:
            if result.error is None:
                decoded.update(decode_group_values(result.group, result.registers, result.bits))
        # Re-order into the requested tag order; groups are ordered by function.
        values = {tag.name: decoded[tag.name] for tag in self._tags if tag.name in decoded}
        # A dropped connection stops later groups too, so sum what we got.
        rtt_ms = round(sum(result.elapsed_ms for result in results), 2)
        error = _error_text(failed[0].error) if failed else None
        return quality, values, rtt_ms, error

    async def _reconnect(self, now: float) -> str:
        """Connect when the backoff is due and return this cycle's error text.

        A failure schedules the next attempt from the capped step ladder,
        jittered by ``0.8 + 0.4 * rand()``; a success resets the ladder and
        makes a later reconnect immediate.
        """
        if now < self._next_attempt_at:
            return self._last_error or "not connected"
        try:
            await self._client.connect()
        except (DeltaPLCError, OSError) as exc:
            step = min(self._backoff_step, len(_BACKOFF_STEPS) - 1)
            delay = _BACKOFF_STEPS[step] * (0.8 + 0.4 * self._rand())
            self._backoff_step += 1
            self._next_attempt_at = now + delay
            self._last_error = _error_text(exc)
            return self._last_error
        self._backoff_step = 0
        self._next_attempt_at = now
        self._last_error = None
        return "not connected"
