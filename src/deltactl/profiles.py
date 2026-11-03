"""Hardware memory architecture profiles and D1001 model registry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from .errors import DeltaValueError, UnknownModelCodeError

__all__ = [
    "DEFAULT_PROFILE",
    "DEVICE_VIEWS",
    "PROFILE_ES2_EX2",
    "PROFILE_ES_EX_SS",
    "PROFILE_SA_SX_SC",
    "PROFILE_SE_EH3_SV2",
    "PROFILE_SS2",
    "Device",
    "DeviceSpec",
    "Segment",
    "SeriesProfile",
    "View",
    "ViewSpec",
    "decode_d1001",
    "get_profile",
    "intersect_profiles",
    "list_profiles",
]


class Device(str, Enum):
    """Delta DVP memory devices handled by the driver."""

    D = "D"
    M = "M"
    X = "X"
    Y = "Y"


class View(str, Enum):
    """The bit or word face of a device."""

    BIT = "bit"
    WORD = "word"


@dataclass(frozen=True)
class Segment:
    """One contiguous index range of a view and its wire anchor.

    ``wire`` is the Modbus address of ``first``; subsequent indices advance by
    ``step``. ``intercept`` normalizes that to ``wire(index) = intercept + index * step``.
    """

    first: int
    last: int
    wire: int
    step: int = 1
    value_width: int = 1
    default_dtype: str | None = None
    coalesce: bool = True

    @property
    def intercept(self) -> int:
        """Wire offset such that ``wire_at(index) == intercept + index * step``."""
        return self.wire - self.first * self.step

    def wire_at(self, index: int) -> int:
        """Modbus wire address of ``index`` in this segment."""
        return self.intercept + index * self.step


@dataclass(frozen=True)
class ViewSpec:
    """One addressing view (bit or word) of a device."""

    function: int
    writable: bool
    segments: tuple[Segment, ...]
    guards: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class DeviceSpec:
    """Every view a device exposes, keyed by view kind."""

    views: Mapping[View, ViewSpec]


DEVICE_VIEWS: Mapping[Device, tuple[View, ...]] = MappingProxyType(
    {
        Device.D: (View.WORD,),
        Device.M: (View.BIT,),
        Device.X: (View.BIT,),
        Device.Y: (View.BIT,),
    }
)


@dataclass(frozen=True)
class SeriesProfile:
    """Hardware profile: a name, its aliases, and the device address specs.

    The ``devices`` mapping is the single source of truth for every device's
    segments, wire anchors, guards, and write access.  Profiles are
    intentionally unhashable: ``devices`` is a mutable mapping and therefore
    cannot participate in ``__hash__``.  Compare profiles with ``==`` instead
    of using them as set members or dict keys.
    """

    name: str
    aliases: tuple[str, ...]
    devices: Mapping[Device, DeviceSpec]


def _split_segments(
    ranges: tuple[tuple[int, int], ...],
    split: int | None,
    low_wire: int,
    high_wire: int,
) -> tuple[Segment, ...]:
    """Split flat index ranges at ``split``, re-anchoring the high half at ``high_wire``."""
    out: list[Segment] = []
    for lo, hi in ranges:
        if split is None or hi <= split:
            out.append(Segment(lo, hi, low_wire + lo))
        elif lo > split:
            out.append(Segment(lo, hi, high_wire + (lo - (split + 1))))
        else:
            out.append(Segment(lo, split, low_wire + lo))
            out.append(Segment(split + 1, hi, high_wire))
    return tuple(out)


def _d_spec(
    ranges: tuple[tuple[int, int], ...],
    split: int | None,
    guards: tuple[tuple[int, int], ...],
) -> ViewSpec:
    """D word view (FC03): ranges split at ``split`` onto the 0x1000/0x9000 blocks."""
    return ViewSpec(3, True, _split_segments(ranges, split, 0x1000, 0x9000), guards)


def _m_spec(
    ranges: tuple[tuple[int, int], ...],
    split: int | None,
    guards: tuple[tuple[int, int], ...],
) -> ViewSpec:
    """M bit view (FC01): ranges split at ``split`` onto the 0x0800/0xB000 blocks."""
    return ViewSpec(1, True, _split_segments(ranges, split, 0x0800, 0xB000), guards)


def _x_spec(max_octal: int) -> ViewSpec:
    """X bit view (FC02): read-only, one contiguous 0x0400 block."""
    return ViewSpec(2, False, (Segment(0, max_octal, 0x0400),))


def _y_spec(max_octal: int) -> ViewSpec:
    """Y bit view (FC01): writable, one contiguous 0x0500 block."""
    return ViewSpec(1, True, (Segment(0, max_octal, 0x0500),))


def _devices(
    *,
    d: ViewSpec,
    m: ViewSpec,
    x: ViewSpec,
    y: ViewSpec,
) -> Mapping[Device, DeviceSpec]:
    """Assemble a read-only device mapping from each device's single view."""
    return MappingProxyType(
        {
            Device.D: DeviceSpec(MappingProxyType({View.WORD: d})),
            Device.M: DeviceSpec(MappingProxyType({View.BIT: m})),
            Device.X: DeviceSpec(MappingProxyType({View.BIT: x})),
            Device.Y: DeviceSpec(MappingProxyType({View.BIT: y})),
        }
    )


_DEFAULT_GUARDED_D: tuple[tuple[int, int], ...] = (
    (1000, 1002),
    (1010, 1012),
    (1067, 1067),
)
_DEFAULT_GUARDED_M: tuple[tuple[int, int], ...] = (
    (1000, 1003),
    (1067, 1067),
)

PROFILE_SE_EH3_SV2 = SeriesProfile(
    name="SE_EH3_SV2",
    aliases=(
        "SE",
        "12SE",
        "28SE",
        "DVP_12SE",
        "DVP_28SE",
        "EH3",
        "DVP_EH3",
        "SV2",
        "28SV2",
        "DVP_28SV2",
        "DVP_SE",
    ),
    devices=_devices(
        d=_d_spec(((0, 11999),), 4095, _DEFAULT_GUARDED_D),
        m=_m_spec(((0, 4095),), 1535, _DEFAULT_GUARDED_M),
        x=_x_spec(0o377),
        y=_y_spec(0o377),
    ),
)

PROFILE_ES2_EX2 = SeriesProfile(
    name="ES2_EX2",
    aliases=(
        "ES2",
        "EX2",
        "DVP_ES2",
        "DVP_EX2",
        "SA2",
        "12SA2",
        "DVP_12SA2",
        "SX2",
        "20SX2",
        "DVP_20SX2",
        "EH2",
        "DVP_EH2",
        "SV",
        "28SV",
        "DVP_28SV",
    ),
    devices=_devices(
        d=_d_spec(((0, 9999),), 4095, _DEFAULT_GUARDED_D),
        m=_m_spec(((0, 4095),), 1535, _DEFAULT_GUARDED_M),
        x=_x_spec(0o377),
        y=_y_spec(0o377),
    ),
)

PROFILE_SS2 = SeriesProfile(
    name="SS2",
    aliases=(
        "SS2",
        "14SS2",
        "DVP_SS2",
        "DVP_14SS2",
    ),
    devices=_devices(
        d=_d_spec(((0, 4999),), 4095, _DEFAULT_GUARDED_D),
        m=_m_spec(((0, 4095),), 1535, _DEFAULT_GUARDED_M),
        x=_x_spec(0o377),
        y=_y_spec(0o377),
    ),
)

PROFILE_SA_SX_SC = SeriesProfile(
    name="SA_SX_SC",
    aliases=(
        "SA",
        "DVP_SA",
        "SX",
        "DVP_SX",
        "SC",
        "DVP_SC",
    ),
    devices=_devices(
        d=_d_spec(((0, 4999),), 4095, _DEFAULT_GUARDED_D),
        m=_m_spec(((0, 4095),), 1535, _DEFAULT_GUARDED_M),
        x=_x_spec(0o177),
        y=_y_spec(0o177),
    ),
)

PROFILE_ES_EX_SS = SeriesProfile(
    name="ES_EX_SS",
    aliases=(
        "ES",
        "DVP_ES",
        "EX",
        "DVP_EX",
        "SS",
        "DVP_SS",
    ),
    devices=_devices(
        d=_d_spec(((0, 599), (1000, 1311)), None, _DEFAULT_GUARDED_D),
        m=_m_spec(((0, 1279),), None, _DEFAULT_GUARDED_M),
        x=_x_spec(0o177),
        y=_y_spec(0o177),
    ),
)

DEFAULT_PROFILE: SeriesProfile = PROFILE_SE_EH3_SV2

_CANONICAL_PROFILES: tuple[SeriesProfile, ...] = (
    PROFILE_SE_EH3_SV2,
    PROFILE_ES2_EX2,
    PROFILE_SS2,
    PROFILE_SA_SX_SC,
    PROFILE_ES_EX_SS,
)

_PROFILE_MAP: dict[str, SeriesProfile] = {}
for _p in _CANONICAL_PROFILES:
    _PROFILE_MAP[_p.name] = _p
    for _alias in _p.aliases:
        _PROFILE_MAP[_alias] = _p


def list_profiles() -> list[str]:
    """Return sorted list of all supported canonical profile names and aliases."""
    return sorted(_PROFILE_MAP.keys())


def get_profile(identifier: str) -> SeriesProfile:
    """Resolve a series identifier or alias to its canonical SeriesProfile.

    Args:
        identifier: Profile name or model alias (case-insensitive, hyphens/underscores allowed).

    Returns:
        The matched SeriesProfile.

    Raises:
        DeltaValueError: If the identifier is unmapped.
    """
    cleaned = identifier.strip().upper().replace("-", "_")
    if cleaned.startswith("DVP_"):
        cleaned = cleaned[4:]
    elif cleaned.startswith("DVP"):
        cleaned = cleaned[3:]

    if cleaned in _PROFILE_MAP:
        return _PROFILE_MAP[cleaned]

    raise DeltaValueError(f"unknown PLC series {identifier!r}; valid: {list_profiles()}")


_D1001_REGISTRY: dict[int, SeriesProfile] = {
    0x7C: PROFILE_SE_EH3_SV2,
    0x70: PROFILE_ES2_EX2,
    0x72: PROFILE_SS2,
    0x74: PROFILE_ES2_EX2,
    0x76: PROFILE_ES2_EX2,
    0x62: PROFILE_SE_EH3_SV2,
    0x66: PROFILE_SE_EH3_SV2,
    0x60: PROFILE_ES2_EX2,
    0x64: PROFILE_ES2_EX2,
    0x50: PROFILE_ES_EX_SS,
    0x52: PROFILE_ES_EX_SS,
    0x54: PROFILE_SA_SX_SC,
    0x58: PROFILE_SA_SX_SC,
    0x5A: PROFILE_SA_SX_SC,
}


def decode_d1001(raw: int) -> tuple[SeriesProfile, int]:
    """Decode a 16-bit D1001 register value into a canonical profile and firmware version.

    Args:
        raw: 16-bit unsigned integer from D1001.

    Returns:
        A tuple of (SeriesProfile, firmware_version_byte).

    Raises:
        UnknownModelCodeError: If the upper byte does not match any known Delta PLC model.
    """
    upper_byte = (raw >> 8) & 0xFF
    firmware_byte = raw & 0xFF
    profile = _D1001_REGISTRY.get(upper_byte)
    if profile is None:
        raise UnknownModelCodeError(
            f"unrecognized model code 0x{upper_byte:02X} in D1001=0x{raw:04X}"
        )
    return profile, firmware_byte


def _union_ranges(
    r1: tuple[tuple[int, int], ...],
    r2: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    all_intervals = sorted(list(r1) + list(r2))
    if not all_intervals:
        return ()
    merged: list[tuple[int, int]] = [all_intervals[0]]
    for start, end in all_intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _segment_key(segment: Segment, function: int) -> tuple[object, ...]:
    """Identity of a segment's wire mapping; two views may only be merged when equal."""
    return (
        function,
        segment.step,
        segment.intercept,
        segment.value_width,
        segment.default_dtype,
        segment.coalesce,
    )


def _intersect_segments(a: ViewSpec, b: ViewSpec) -> tuple[Segment, ...]:
    """Intersect every pair of segments that share a wire-mapping key.

    All pairs are considered rather than one pair per key: a view may hold
    several disjoint segments with the same key, and each must survive.
    """
    out: list[Segment] = []
    for sa in a.segments:
        key = _segment_key(sa, a.function)
        for sb in b.segments:
            if _segment_key(sb, b.function) != key:
                continue
            first = max(sa.first, sb.first)
            last = min(sa.last, sb.last)
            if first > last:
                continue
            out.append(
                Segment(
                    first,
                    last,
                    sa.intercept + first * sa.step,
                    step=sa.step,
                    value_width=sa.value_width,
                    default_dtype=sa.default_dtype,
                    coalesce=sa.coalesce,
                )
            )
    out.sort(key=lambda segment: segment.first)
    return tuple(out)


def _intersect_view_specs(a: ViewSpec, b: ViewSpec) -> ViewSpec | None:
    """Intersected view spec, or ``None`` when no segment survives."""
    segments = _intersect_segments(a, b)
    if not segments:
        return None
    return ViewSpec(
        a.function,
        a.writable and b.writable,
        segments,
        _union_ranges(a.guards, b.guards),
    )


def _intersect_devices(
    a: Mapping[Device, DeviceSpec],
    b: Mapping[Device, DeviceSpec],
) -> Mapping[Device, DeviceSpec]:
    """Intersect the views both profiles expose, dropping empty devices and views."""
    devices: dict[Device, DeviceSpec] = {}
    for device, spec_a in a.items():
        spec_b = b.get(device)
        if spec_b is None:
            continue
        views: dict[View, ViewSpec] = {}
        for view, view_a in spec_a.views.items():
            view_b = spec_b.views.get(view)
            if view_b is None:
                continue
            merged = _intersect_view_specs(view_a, view_b)
            if merged is not None:
                views[view] = merged
        if views:
            devices[device] = DeviceSpec(MappingProxyType(views))
    return MappingProxyType(devices)


def intersect_profiles(a: SeriesProfile, b: SeriesProfile) -> SeriesProfile:
    """Calculate the formal mathematical intersection of two SeriesProfiles.

    Devices and views are intersected by pairing every segment of ``a`` with
    every segment of ``b`` that shares its wire-mapping key
    ``(function, step, intercept, value_width, default_dtype, coalesce)``.
    Matching pairs intersect their index ranges; guards are unioned and a view
    is writable only if both profiles allow writes.

    A device or view with no surviving segment is dropped, never repopulated,
    so intersecting disjoint profiles yields an empty ``devices`` mapping.
    """
    return SeriesProfile(
        name=f"{a.name}&{b.name}",
        aliases=(),
        devices=_intersect_devices(a.devices, b.devices),
    )
