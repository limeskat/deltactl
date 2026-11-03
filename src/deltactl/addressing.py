"""Delta DVP tag grammar, wire map, guards, raw decoding, and range expansion."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from .errors import (
    AddressRangeError,
    AddressSyntaxError,
    BlockBoundaryError,
    DeltaAddressError,
    ReadOnlyAddressError,
)
from .profiles import (
    DEFAULT_PROFILE,
    DEVICE_VIEWS,
    Device,
    Segment,
    SeriesProfile,
    View,
    ViewSpec,
)

__all__ = [
    "D_GUARDS",
    "M_GUARDS",
    "Address",
    "Device",
    "ParsedTag",
    "TagAddress",
    "View",
    "block_for",
    "expand_tags",
    "normalize_tag",
    "parse_raw",
    "parse_tag",
    "validate_span",
    "wire_for",
]


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
    view: View
    function: int
    segment: Segment
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

    @property
    def index(self) -> int:
        return self.address.index

    @property
    def device(self) -> Device:
        return self.address.device

    @property
    def wire(self) -> int:
        return self.address.wire

    @property
    def block(self) -> int:
        return self.address.block

    @property
    def bit(self) -> int | None:
        return self.address.bit

    @property
    def view(self) -> View:
        return self.address.view

    @property
    def function(self) -> int:
        return self.address.function

    @property
    def value_width(self) -> int:
        return self.address.segment.value_width

    @property
    def default_dtype(self) -> str | None:
        return self.address.segment.default_dtype

    @property
    def coalesce(self) -> bool:
        return self.address.segment.coalesce


TagAddress = Address | ParsedTag


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


def wire_for(
    device: Device,
    index: int,
    *,
    view: View | None = None,
    profile: SeriesProfile | None = None,
) -> int:
    """Modbus wire address for ``index`` within ``device`` and ``view``."""
    segment, _ = _locate(device, index, view, profile)
    return segment.wire_at(index)


def block_for(
    device: Device,
    index: int,
    *,
    view: View | None = None,
    profile: SeriesProfile | None = None,
) -> int:
    """Planning block id for ``index``: its wire anchor's rank within the view."""
    segment, view_spec = _locate(device, index, view, profile)
    return _block_of(segment, view_spec)


def _block_of(segment: Segment, view_spec: ViewSpec) -> int:
    """Ordinal of ``segment``'s intercept among the view's distinct intercepts."""
    intercepts = list(dict.fromkeys(s.intercept for s in view_spec.segments))
    return intercepts.index(segment.intercept)


def _resolve_view(device: Device, view: View | None) -> View:
    """Infer the only view of a single-view device, else require an explicit one."""
    try:
        views = DEVICE_VIEWS[device]
    except KeyError:
        raise ValueError(f"unknown device {device!r}") from None
    if view is None:
        if len(views) > 1:
            names = ", ".join(v.value for v in views)
            raise ValueError(
                f"{device.value} has multiple views ({names}); specify view= explicitly"
            )
        return views[0]
    try:
        resolved = View(view)
    except ValueError:
        names = ", ".join(v.value for v in views)
        raise ValueError(
            f"unknown view {view!r}: {device.value} has {names}"
        ) from None
    if resolved not in views:
        names = ", ".join(v.value for v in views)
        raise ValueError(
            f"{device.value} has no {resolved.value} view; valid: {names}"
        )
    return resolved


def _locate(
    device: Device,
    index: int,
    view: View | None,
    profile: SeriesProfile | None,
) -> tuple[Segment, ViewSpec]:
    """Segment containing ``index`` and its view spec."""
    prof = profile or DEFAULT_PROFILE
    resolved = _resolve_view(device, view)
    view_spec = prof.devices[device].views[resolved]
    for segment in view_spec.segments:
        if segment.first <= index <= segment.last:
            return segment, view_spec
    ranges_str = ", ".join(f"{s.first}-{s.last}" for s in view_spec.segments)
    raise AddressRangeError(
        f"{_format_tag(device, index)} is outside the {device.value} range ({ranges_str})"
    )


def parse_tag(text: str, profile: SeriesProfile | None = None) -> ParsedTag:
    """Parse one Delta tag such as ``D100``, ``D100.3``, ``D100:f32``, or ``X17``."""
    prof = profile or DEFAULT_PROFILE
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

    view = _resolve_view(device, None)
    segment, view_spec = _locate(device, index, view, prof)

    address = Address(
        device=device,
        index=index,
        wire=segment.wire_at(index),
        block=_block_of(segment, view_spec),
        view=view,
        function=view_spec.function,
        segment=segment,
        bit=bit,
    )
    display = address.display if dtype is None else f"{address.display}:{dtype}"
    return ParsedTag(address=address, dtype=dtype, display=display)


def normalize_tag(text: str, profile: SeriesProfile | None = None) -> str:
    """Canonical display form of one tag (uppercase device, lowercase dtype)."""
    return parse_tag(text, profile=profile).display


def parse_raw(
    value: str | int, raw_type: str = "d", profile: SeriesProfile | None = None
) -> Address:
    """Decode a 0-based Modbus wire address (decimal or ``0x`` hex) into an Address."""
    prof = profile or DEFAULT_PROFILE
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
            view = _resolve_view(device, None)
            segment, view_spec = _locate(device, index, view, prof)
            if segment.wire_at(index) != wire:
                raise AddressRangeError(
                    f"raw wire address 0x{wire:04X} is not addressable on the {device.value} view"
                )
            return Address(
                device=device,
                index=index,
                wire=wire,
                block=_block_of(segment, view_spec),
                view=view,
                function=view_spec.function,
                segment=segment,
                bit=None,
            )
    expected = " or ".join(f"0x{lo:04X}-0x{hi:04X}" for lo, hi, _ in ranges)
    raise AddressRangeError(
        f"raw wire address 0x{wire:04X} is outside the {device.value} ranges ({expected})"
    )


def _extract_address(target: TagAddress) -> Address:
    if isinstance(target, Address):
        return target
    if hasattr(target, "address") and isinstance(target.address, Address):
        return target.address
    raise TypeError(f"expected Address or ParsedTag, got {type(target)!r}")


def validate_span(
    address: TagAddress,
    width: int | None = None,
    *,
    count: int | None = None,
    write: bool = False,
    allow_block_crossing: bool = False,
    profile: SeriesProfile | None = None,
) -> None:
    """Validate one value's half-open span ``[index, index + span_count)``.

    Order: range/bounds, split-block boundary, then read-only guards. A write
    fails if any address in the entire span is guarded; no PDU is implied.
    """
    addr = _extract_address(address)
    register_count = width if count is None else count
    if register_count is None:
        raise ValueError("span width or count must be specified")
    if register_count < 1:
        raise ValueError(f"span width must be at least 1, got {register_count}")

    prof = profile or DEFAULT_PROFILE
    device = addr.device
    start = addr.index

    try:
        segment, view_spec = _locate(device, start, addr.view, prof)
    except AddressRangeError:
        raise AddressRangeError(
            _span_range_message(device, addr, start, register_count, prof)
        ) from None

    value_width = segment.value_width
    if register_count % value_width:
        raise ValueError(
            f"span of {register_count} registers does not divide evenly into "
            f"{device.value} {addr.view.value} values of width {value_width}"
        )
    index_count = register_count // value_width
    end = start + index_count - 1

    segments = view_spec.segments
    chain = [segment]
    for nxt in segments[segments.index(segment) + 1 :]:
        if nxt.first != chain[-1].last + 1:
            break
        chain.append(nxt)

    if end > chain[-1].last:
        raise AddressRangeError(
            _span_range_message(device, addr, start, register_count, prof)
        )

    if end > segment.last:
        for prev, nxt in pairwise(chain):
            if nxt.first > end:
                break
            if not allow_block_crossing or nxt.value_width != value_width:
                raise _boundary_error(device, addr, end, prev, nxt)

    if write:
        if not view_spec.writable:
            raise ReadOnlyAddressError(_read_only_message(device, start))
        first = _first_guarded(view_spec.guards, start, end)
        if first is not None:
            raise ReadOnlyAddressError(_read_only_message(device, first))


def _span_range_message(
    device: Device,
    addr: Address,
    start: int,
    register_count: int,
    profile: SeriesProfile,
) -> str:
    return (
        f"span of {addr.display} [{start}, {start + register_count}) "
        f"exceeds the {device.value} range for profile {profile.name}"
    )


def _boundary_error(
    device: Device, addr: Address, end: int, prev: Segment, nxt: Segment
) -> BlockBoundaryError:
    split = prev.last + 1
    kind = "block" if nxt.intercept != prev.intercept else "width"
    return BlockBoundaryError(
        f"a single value spanning {addr.display}..{_format_tag(device, end)} "
        f"crosses the {device.value}{split - 1}|{device.value}{split} {kind} boundary"
    )


def _read_only_message(device: Device, index: int) -> str:
    return (
        f"{_format_tag(device, index)} is read-only: "
        f"writes may not include guarded addresses"
    )


def _first_guarded(
    guards: tuple[tuple[int, int], ...], start: int, end: int
) -> int | None:
    """First guarded index overlapped by the inclusive span ``[start, end]``."""
    for lo, hi in guards:
        if start <= hi and end >= lo:
            return max(lo, start)
    return None


def expand_tags(
    items: Sequence[str],
    *,
    limit: int = 1024,
    cap: int | None = None,
    profile: SeriesProfile | None = None,
) -> list[str]:
    """Expand ranges, normalize, dedupe (first occurrence wins), and cap.

    Ranges must use one device and bare endpoints (no bit or dtype suffix); an
    optional single dtype suffix applies to every expanded tag. Raises
    ``ValueError`` for mixed devices, bit endpoints, malformed ranges, or
    expansions beyond ``limit``.
    """
    effective_limit = cap if cap is not None else limit
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
            _expand_range(text, base, dtype, out, seen, effective_limit, profile=profile)
        else:
            _add_unique(normalize_tag(text, profile=profile), out, seen, effective_limit)
    return out


def _add_unique(tag: str, out: list[str], seen: set[str], limit: int) -> None:
    if tag in seen:
        return
    if len(seen) >= limit:
        raise ValueError(f"expansion exceeds the {limit}-tag limit")
    seen.add(tag)
    out.append(tag)


def _expand_range(
    text: str,
    base: str,
    dtype: str | None,
    out: list[str],
    seen: set[str],
    limit: int,
    profile: SeriesProfile | None = None,
) -> None:
    lo_text, _, hi_text = base.partition("-")
    for part in (lo_text, hi_text):
        if not part or "." in part or ":" in part:
            raise ValueError(
                f"range {text!r}: endpoints must be bare tags without bit or "
                f"dtype suffixes (e.g. D100-D109, X0-X17)"
            )
    try:
        lo = parse_tag(lo_text, profile=profile)
        hi = parse_tag(hi_text, profile=profile)
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
    prof = profile or DEFAULT_PROFILE
    for index in range(lo_addr.index, hi_addr.index + 1):
        _locate(device, index, None, prof)
        _add_unique(_format_tag(device, index, dtype=dtype), out, seen, limit)
