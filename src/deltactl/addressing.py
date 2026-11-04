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
    "raw_type_for",
    "validate_span",
    "wire_for",
]


_OCTAL_DEVICES = frozenset({Device.X, Device.Y})

# Read-only guard tuples as inclusive index ranges; enforced over the whole
# write span, so a write touching the guard through its end address fails too.
D_GUARDS: tuple[tuple[int, int], ...] = ((1000, 1002), (1010, 1012), (1067, 1067))
M_GUARDS: tuple[tuple[int, int], ...] = ((1000, 1003), (1067, 1067))

_TAG_RE = re.compile(
    r"^(?P<dev>[DMSXYTC])(?P<index>[0-9]+)"
    r"(?:\.(?P<member>[0-9]+|[CV]))?(?::(?P<dtype>u16|i16|i32|u32|f32))?$",
    re.IGNORECASE,
)

_DTYPES = frozenset({"u16", "i16", "i32", "u32", "f32"})

_VIEW_SUFFIXES: dict[View, str] = {
    View.BIT: ".c",
    View.WORD: ".v",
}

# Modbus raw type token → (Device, View) mapping.
_RAW_DOMAINS: dict[str, tuple[Device, View]] = {
    "d": (Device.D, View.WORD),
    "m": (Device.M, View.BIT),
    "x": (Device.X, View.BIT),
    "y": (Device.Y, View.BIT),
    "s": (Device.S, View.BIT),
    "tc": (Device.T, View.BIT),
    "tv": (Device.T, View.WORD),
    "cc": (Device.C, View.BIT),
    "cv": (Device.C, View.WORD),
}
_DEVICE_VIEW_TO_RAW: dict[tuple[Device, View], str] = {v: k for k, v in _RAW_DOMAINS.items()}


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
        return _format_tag(self.device, self.index, view=self.view, bit=self.bit)

    @property
    def coalesce(self) -> bool:
        return self.segment.coalesce

    @property
    def value_width(self) -> int:
        return self.segment.value_width


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
        return self.address.coalesce


TagAddress = Address | ParsedTag


def _format_tag(
    device: Device,
    index: int,
    *,
    view: View | None = None,
    bit: int | None = None,
    dtype: str | None = None,
) -> str:
    digits = f"{index:o}" if device in _OCTAL_DEVICES else str(index)
    text = f"{device.value}{digits}"
    if len(DEVICE_VIEWS.get(device, ())) > 1:
        if view is not None and view in _VIEW_SUFFIXES:
            text = f"{text}{_VIEW_SUFFIXES[view]}"
    elif bit is not None:
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
        raise ValueError(f"unknown view {view!r}: {device.value} has {names}") from None
    if resolved not in views:
        names = ", ".join(v.value for v in views)
        raise ValueError(f"{device.value} has no {resolved.value} view; valid: {names}")
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
    dev_spec = prof.devices.get(device)
    if dev_spec is None or resolved not in dev_spec.views:
        raise AddressRangeError(
            f"{device.value} {resolved.value} is not available on profile {prof.name}"
        )
    view_spec = dev_spec.views[resolved]
    for segment in view_spec.segments:
        if segment.first <= index <= segment.last:
            return segment, view_spec
    if device in _OCTAL_DEVICES:
        ranges_str = ", ".join(f"{s.first:o}-{s.last:o}" for s in view_spec.segments)
    else:
        ranges_str = ", ".join(f"{s.first}-{s.last}" for s in view_spec.segments)
    raise AddressRangeError(
        f"{_format_tag(device, index, view=resolved)} is outside the {device.value} range ({ranges_str})"
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
    member_text = match["member"]
    dtype_text = match["dtype"]

    if device in _OCTAL_DEVICES:
        if any(ch not in "01234567" for ch in index_text):
            raise AddressSyntaxError(
                f"invalid octal digits in {text!r}: X/Y indices use digits 0-7"
            )
        index = int(index_text, 8)
    else:
        index = int(index_text)

    views = DEVICE_VIEWS[device]
    is_multiview = len(views) > 1

    bit: int | None = None
    view: View

    if is_multiview:
        if member_text is None:
            suffixes = "/".join(_VIEW_SUFFIXES[v] for v in views)
            raise AddressSyntaxError(
                f"{text!r}: {device.value} has multiple views; a view suffix ({suffixes}) is required"
            )
        member_lower = member_text.lower()
        if member_lower == "c":
            view = View.BIT
        elif member_lower == "v":
            view = View.WORD
        else:
            raise AddressSyntaxError(f"{text!r}: numeric member addressing is only supported on D")
    else:
        view = views[0]
        if member_text is not None:
            if device is not Device.D or not member_text.isdigit():
                if device is Device.D:
                    raise AddressSyntaxError(
                        f"{text!r}: view suffixes are not valid on single-view device D"
                    )
                raise AddressSyntaxError(
                    f"{text!r}: member suffix is not valid on single-view device {device.value}"
                )
            bit = int(member_text)
            if bit > 15:
                raise AddressSyntaxError(f"{text!r}: D bit index must be 0..15")

    dtype: str | None = None
    if dtype_text is not None:
        if view is View.BIT or bit is not None:
            raise AddressSyntaxError(f"{text!r}: dtype suffixes are not supported on bit views")
        dtype = dtype_text.lower()

    segment, view_spec = _locate(device, index, view, prof)

    if dtype is not None and segment.default_dtype is not None:
        if dtype == "f32":
            raise AddressSyntaxError(
                f"{text!r}: f32 is not allowed on fixed-width {device.value} values"
            )
        dtype_width = 2 if dtype in ("i32", "u32", "f32") else 1
        if dtype_width != segment.value_width:
            raise AddressSyntaxError(
                f"{text!r}: dtype {dtype} width {dtype_width} does not match "
                f"segment value_width {segment.value_width}"
            )

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


def _extract_address(target: TagAddress) -> Address:
    if isinstance(target, Address):
        return target
    if hasattr(target, "address") and isinstance(target.address, Address):
        return target.address
    raise TypeError(f"expected Address or ParsedTag, got {type(target)!r}")


def raw_type_for(address: TagAddress) -> str:
    """Modbus raw type token corresponding to *address*."""
    addr = _extract_address(address)
    try:
        return _DEVICE_VIEW_TO_RAW[(addr.device, addr.view)]
    except KeyError:
        raise ValueError(f"no raw type for {addr.device.value} {addr.view.value}") from None


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
        device, view = _RAW_DOMAINS[raw_type.lower()]
    except (AttributeError, KeyError):
        valid = "|".join(_RAW_DOMAINS)
        raise ValueError(f"unknown raw type {raw_type!r}: expected {valid}") from None
    view_spec = prof.devices[device].views[view]
    for segment in view_spec.segments:
        lo = segment.wire_at(segment.first)
        hi = segment.wire_at(segment.last) + segment.value_width - 1
        if lo <= wire <= hi:
            if (wire - segment.intercept) % segment.step == 0:
                index = (wire - segment.intercept) // segment.step
                if segment.first <= index <= segment.last:
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
            raise AddressRangeError(
                f"raw wire address 0x{wire:04X} is not addressable on the {device.value} view"
            )
    expected = " or ".join(
        f"0x{s.wire_at(s.first):04X}-0x{s.wire_at(s.last) + s.value_width - 1:04X}"
        for s in view_spec.segments
    )
    raise AddressRangeError(
        f"raw wire address 0x{wire:04X} is outside the {device.value} ranges ({expected})"
    )


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
        index_count = register_count // addr.segment.value_width
        raise AddressRangeError(
            _span_range_message(device, addr, start, index_count, prof)
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
        raise AddressRangeError(_span_range_message(device, addr, start, index_count, prof))

    if end > segment.last:
        for prev, nxt in pairwise(chain):
            if nxt.first > end:
                break
            if not allow_block_crossing or nxt.value_width != value_width:
                raise _boundary_error(device, addr, end, prev, nxt)

    if write:
        if not view_spec.writable:
            raise ReadOnlyAddressError(_read_only_message(device, start, view=addr.view))
        first = _first_guarded(view_spec.guards, start, end)
        if first is not None:
            raise ReadOnlyAddressError(_read_only_message(device, first, view=addr.view))


def _span_range_message(
    device: Device,
    addr: Address,
    start: int,
    index_count: int,
    profile: SeriesProfile,
) -> str:
    return (
        f"span of {addr.display} [{start}, {start + index_count}) "
        f"exceeds the {device.value} range for profile {profile.name}"
    )


def _boundary_error(
    device: Device, addr: Address, end: int, prev: Segment, nxt: Segment
) -> BlockBoundaryError:
    split = prev.last + 1
    kind = "block" if nxt.intercept != prev.intercept else "width"
    return BlockBoundaryError(
        f"a single value spanning {addr.display}..{_format_tag(device, end, view=addr.view)} "
        f"crosses the {device.value}{split - 1}|{device.value}{split} {kind} boundary"
    )


def _read_only_message(device: Device, index: int, view: View | None = None) -> str:
    return (
        f"{_format_tag(device, index, view=view)} is read-only: "
        f"writes may not include guarded addresses"
    )


def _first_guarded(guards: tuple[tuple[int, int], ...], start: int, end: int) -> int | None:
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
        if not part or ":" in part:
            raise ValueError(f"range {text!r}: endpoints must not contain dtype suffixes")
    try:
        lo = parse_tag(lo_text, profile=profile)
        hi = parse_tag(hi_text, profile=profile)
    except DeltaAddressError as exc:
        raise ValueError(
            f"invalid range {text!r}: both endpoints must be valid tags ({exc})"
        ) from None
    if lo.address.bit is not None or hi.address.bit is not None:
        raise ValueError(f"range {text!r}: bit offsets are not allowed in range endpoints")
    lo_addr, hi_addr = lo.address, hi.address
    if lo_addr.device is not hi_addr.device:
        raise ValueError(
            f"range {text!r} spans multiple devices: {lo_addr.display} and {hi_addr.display}"
        )
    if lo_addr.view is not hi_addr.view:
        raise ValueError(
            f"range {text!r} spans multiple views: {lo_addr.display} and {hi_addr.display}"
        )
    if lo.value_width != hi.value_width:
        raise ValueError(
            f"range {text!r} spans mixed value widths: {lo.value_width} != {hi.value_width}"
        )
    if dtype is not None:
        if lo_addr.view is not View.WORD:
            raise ValueError(f"range {text!r}: dtype suffixes are only valid on word ranges")
        if lo.address.segment.default_dtype is not None:
            if dtype == "f32":
                raise ValueError(
                    f"range {text!r}: f32 is not allowed on fixed-width {lo_addr.device.value} values"
                )
            dtype_width = 2 if dtype in ("i32", "u32", "f32") else 1
            if dtype_width != lo.value_width:
                raise ValueError(
                    f"range {text!r}: dtype {dtype} width {dtype_width} does not match "
                    f"segment value_width {lo.value_width}"
                )
    if hi_addr.index < lo_addr.index:
        raise ValueError(f"range {text!r} is reversed: {lo_addr.display} > {hi_addr.display}")
    count = hi_addr.index - lo_addr.index + 1
    if len(seen) + count > limit:
        raise ValueError(f"expanding range {text!r} exceeds the {limit}-tag limit")
    device = lo_addr.device
    prof = profile or DEFAULT_PROFILE
    for index in range(lo_addr.index, hi_addr.index + 1):
        seg, _ = _locate(device, index, lo_addr.view, prof)
        if seg.value_width != lo.value_width:
            raise ValueError(
                f"range {text!r} spans mixed value widths: segment width {seg.value_width} != {lo.value_width}"
            )
        _add_unique(
            _format_tag(device, index, view=lo_addr.view, dtype=dtype),
            out,
            seen,
            limit,
        )
