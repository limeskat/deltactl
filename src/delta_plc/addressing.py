"""Delta DVP tag grammar, wire map, guards, raw decoding, and range expansion."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from .errors import (
    AddressRangeError,
    AddressSyntaxError,
    BlockBoundaryError,
    DeltaAddressError,
    ReadOnlyAddressError,
)

__all__ = [
    "D_GUARDS",
    "M_GUARDS",
    "Address",
    "Device",
    "ParsedTag",
    "block_for",
    "expand_tags",
    "normalize_tag",
    "parse_raw",
    "parse_tag",
    "validate_span",
    "wire_for",
]


class Device(str, Enum):
    """Delta DVP memory devices handled by the driver."""

    D = "D"
    M = "M"
    X = "X"
    Y = "Y"


_D_SPLIT = 4096  # D0–D4095 is block 0; D4096–D11999 is block 1
_M_SPLIT = 1536  # M0–M1535 is block 0; M1536–M4095 is block 1

_MAX_INDEX: dict[Device, int] = {
    Device.D: 11999,
    Device.M: 4095,
    Device.X: 0o377,
    Device.Y: 0o377,
}

_OCTAL_DEVICES = frozenset({Device.X, Device.Y})

# Read-only guard tuples as inclusive index ranges; enforced over the whole
# write span, so a write touching the guard through its end address fails too.
D_GUARDS: tuple[tuple[int, int], ...] = ((1000, 1002), (1010, 1012), (1067, 1067))
M_GUARDS: tuple[tuple[int, int], ...] = ((1000, 1003), (1067, 1067))

_TAG_RE = re.compile(
    r"^(?P<dev>[DdMmXxYy])(?P<index>[0-9]+)"
    r"(?:\.(?P<bit>[0-9]+))?(?::(?P<dtype>u16|i16|i32|u32|f32))?$",
    re.IGNORECASE,
)

_DTYPES = frozenset({"u16", "i16", "i32", "u32", "f32"})

# Wire range → symbolic index for parse_raw: (lo, hi, index_base) per mapped block.
_RAW_DOMAINS: dict[str, tuple[Device, tuple[tuple[int, int, int], ...]]] = {
    "d": (Device.D, ((4096, 8191, 0), (36864, 44767, 4096))),
    "m": (Device.M, ((2048, 3583, 0), (45056, 47615, 1536))),
    "x": (Device.X, ((1024, 1279, 0),)),
    "y": (Device.Y, ((1280, 1535, 0),)),
}


@dataclass(frozen=True)
class Address:
    """A resolved Delta address: symbolic index plus its Modbus wire number."""

    device: Device
    index: int
    wire: int
    block: int
    bit: int | None = None

    @property
    def display(self) -> str:
        """Canonical base tag: uppercase device, no leading zeros, octal for X/Y."""
        return _format_tag(self.device, self.index, bit=self.bit)


@dataclass(frozen=True)
class ParsedTag:
    """A successfully parsed tag with its resolved address and dtype."""

    address: Address
    dtype: str | None
    display: str


def _format_tag(
    device: Device, index: int, *, bit: int | None = None, dtype: str | None = None
) -> str:
    digits = f"{index:o}" if device in _OCTAL_DEVICES else str(index)
    text = f"{device.value}{digits}"
    if bit is not None:
        text = f"{text}.{bit}"
    if dtype is not None:
        text = f"{text}:{dtype}"
    return text


def wire_for(device: Device, index: int) -> int:
    """Modbus wire address for ``index`` within ``device``."""
    _check_index(device, index)
    if device is Device.D:
        return 0x1000 + index if index < _D_SPLIT else 0x9000 + index - _D_SPLIT
    if device is Device.M:
        return 0x0800 + index if index < _M_SPLIT else 0xB000 + index - _M_SPLIT
    if device is Device.X:
        return 0x0400 + index
    return 0x0500 + index


def block_for(device: Device, index: int) -> int:
    """Planning block id (0 or 1) for ``index`` within ``device``."""
    _check_index(device, index)
    if device is Device.D:
        return 0 if index < _D_SPLIT else 1
    if device is Device.M:
        return 0 if index < _M_SPLIT else 1
    return 0


def _check_index(device: Device, index: int) -> None:
    if not 0 <= index <= _MAX_INDEX[device]:
        raise AddressRangeError(
            f"{device.value}{index} is outside the {device.value} range (max {_MAX_INDEX[device]})"
        )


def parse_tag(text: str) -> ParsedTag:
    """Parse one Delta tag such as ``D100``, ``D100.3``, ``D100:f32``, or ``X17``."""
    match = _TAG_RE.fullmatch(text.strip())
    if match is None:
        raise AddressSyntaxError(
            f"invalid Delta tag {text!r}: expected e.g. D100, D100.3, D100:f32, or X17"
        )
    device = Device(match["dev"].upper())
    index_text = match["index"]
    bit_text = match["bit"]
    dtype_text = match["dtype"]

    if device in _OCTAL_DEVICES:
        if any(ch not in "01234567" for ch in index_text):
            raise AddressSyntaxError(
                f"invalid octal digits in {text!r}: X/Y indices use digits 0-7"
            )
        index = int(index_text, 8)
    else:
        index = int(index_text)

    bit: int | None = None
    if bit_text is not None:
        if device is not Device.D:
            raise AddressSyntaxError(f"{text!r}: bit addressing is only supported on D")
        bit = int(bit_text)
        if bit > 15:
            raise AddressSyntaxError(f"{text!r}: D bit index must be 0..15")

    dtype: str | None = None
    if dtype_text is not None:
        if device is not Device.D:
            raise AddressSyntaxError(f"{text!r}: dtype suffixes are only supported on D")
        dtype = dtype_text.lower()
        if bit is not None:
            raise AddressSyntaxError(
                f"{text!r}: bit addressing and a dtype suffix cannot be combined"
            )

    if index > _MAX_INDEX[device]:
        raise AddressRangeError(
            f"{text!r} exceeds the {device.value} range (max {_MAX_INDEX[device]})"
        )

    address = Address(
        device=device,
        index=index,
        wire=wire_for(device, index),
        block=block_for(device, index),
        bit=bit,
    )
    display = address.display if dtype is None else f"{address.display}:{dtype}"
    return ParsedTag(address=address, dtype=dtype, display=display)


def normalize_tag(text: str) -> str:
    """Canonical display form of one tag (uppercase device, lowercase dtype)."""
    return parse_tag(text).display


def parse_raw(value: str | int, raw_type: str = "d") -> Address:
    """Decode a 0-based Modbus wire address (decimal or ``0x`` hex) into an Address."""
    if isinstance(value, int):
        wire = value
    else:
        wire = int(value, 0)
    try:
        device, ranges = _RAW_DOMAINS[raw_type.lower()]
    except (AttributeError, KeyError):
        raise ValueError(f"unknown raw type {raw_type!r}: expected d|m|x|y") from None
    for lo, hi, base in ranges:
        if lo <= wire <= hi:
            index = base + (wire - lo)
            return Address(
                device=device, index=index, wire=wire, block=block_for(device, index), bit=None
            )
    expected = " or ".join(f"0x{lo:04X}-0x{hi:04X}" for lo, hi, _ in ranges)
    raise AddressRangeError(
        f"raw wire address 0x{wire:04X} is outside the {device.value} ranges ({expected})"
    )


def validate_span(
    address: Address, width: int, *, write: bool, allow_block_crossing: bool = False
) -> None:
    """Validate one value's half-open span ``[index, index + width)``.

    Order: range/bounds, split-block boundary, then read-only guards. A write
    fails if any address in the entire span is guarded; no PDU is implied.
    """
    if width < 1:
        raise ValueError(f"span width must be at least 1, got {width}")
    device = address.device
    end = address.index + width - 1
    if address.index < 0 or end > _MAX_INDEX[device]:
        raise AddressRangeError(
            f"span of {address.display} [{address.index}, {address.index + width}) "
            f"exceeds the {device.value} range (max {_MAX_INDEX[device]})"
        )
    if not allow_block_crossing and block_for(device, address.index) != block_for(device, end):
        split = _D_SPLIT if device is Device.D else _M_SPLIT
        raise BlockBoundaryError(
            f"a single value spanning {address.display}..{_format_tag(device, end)} "
            f"crosses the {device.value}{split - 1}|{device.value}{split} block boundary"
        )
    if write:
        first = _first_guarded(device, address.index, end)
        if first is not None:
            raise ReadOnlyAddressError(
                f"{_format_tag(device, first)} is read-only: writes may not include guarded addresses"
            )


def _first_guarded(device: Device, start: int, end: int) -> int | None:
    """First guarded index overlapped by the inclusive span ``[start, end]``."""
    if device is Device.X:
        return start  # X0–X377 is entirely read-only
    guards: tuple[tuple[int, int], ...] = ()
    if device is Device.D:
        guards = D_GUARDS
    elif device is Device.M:
        guards = M_GUARDS
    for lo, hi in guards:
        if start <= hi and end >= lo:
            return max(lo, start)
    return None


def expand_tags(items: Sequence[str], *, limit: int = 1024) -> list[str]:
    """Expand ranges, normalize, dedupe (first occurrence wins), and cap.

    Ranges must use one device and bare endpoints (no bit or dtype suffix); an
    optional single dtype suffix applies to every expanded tag. Raises
    ``ValueError`` for mixed devices, bit endpoints, malformed ranges, or
    expansions beyond ``limit``.
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = item.strip()
        head, sep, suffix = text.partition(":")
        dtype: str | None
        if sep and suffix.lower() in _DTYPES:
            base, dtype = head, suffix.lower()
        else:
            base, dtype = text, None
        if "-" in base:
            _expand_range(text, base, dtype, out, seen, limit)
        else:
            _add_unique(normalize_tag(text), out, seen, limit)
    return out


def _add_unique(tag: str, out: list[str], seen: set[str], limit: int) -> None:
    if tag in seen:
        return
    if len(seen) >= limit:
        raise ValueError(f"expansion exceeds the {limit}-tag limit")
    seen.add(tag)
    out.append(tag)


def _expand_range(
    text: str, base: str, dtype: str | None, out: list[str], seen: set[str], limit: int
) -> None:
    lo_text, _, hi_text = base.partition("-")
    for part in (lo_text, hi_text):
        if not part or "." in part or ":" in part:
            raise ValueError(
                f"range {text!r}: endpoints must be bare tags without bit or "
                f"dtype suffixes (e.g. D100-D109, X0-X17)"
            )
    try:
        lo = parse_tag(lo_text)
        hi = parse_tag(hi_text)
    except DeltaAddressError as exc:
        raise ValueError(
            f"invalid range {text!r}: both endpoints must be valid bare tags ({exc})"
        ) from None
    lo_addr, hi_addr = lo.address, hi.address
    if lo_addr.device is not hi_addr.device:
        raise ValueError(
            f"range {text!r} spans multiple devices: {lo_addr.display} and {hi_addr.display}"
        )
    if dtype is not None and lo_addr.device is not Device.D:
        raise ValueError(f"range {text!r}: dtype suffixes are only valid on D ranges")
    if hi_addr.index < lo_addr.index:
        raise ValueError(f"range {text!r} is reversed: {lo_addr.display} > {hi_addr.display}")
    count = hi_addr.index - lo_addr.index + 1
    if len(seen) + count > limit:
        raise ValueError(f"expanding range {text!r} exceeds the {limit}-tag limit")
    device = lo_addr.device
    for index in range(lo_addr.index, hi_addr.index + 1):
        _add_unique(_format_tag(device, index, dtype=dtype), out, seen, limit)
