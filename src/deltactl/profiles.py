"""Hardware memory architecture profiles and D1001 model registry."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import DeltaValueError, UnknownModelCodeError

__all__ = [
    "DEFAULT_PROFILE",
    "PROFILE_ES2_EX2",
    "PROFILE_ES_EX_SS",
    "PROFILE_SA_SX_SC",
    "PROFILE_SE_EH3_SV2",
    "PROFILE_SS2",
    "SeriesProfile",
    "decode_d1001",
    "get_profile",
    "intersect_profiles",
    "list_profiles",
]


@dataclass(frozen=True)
class SeriesProfile:
    """Hardware profile specifying address limits and wire boundaries."""

    name: str
    aliases: tuple[str, ...]
    d_ranges: tuple[tuple[int, int], ...]
    d_split_boundary: int | None
    m_ranges: tuple[tuple[int, int], ...]
    m_split_boundary: int | None
    x_max_octal: int
    y_max_octal: int
    guarded_d: tuple[tuple[int, int], ...]
    guarded_m: tuple[tuple[int, int], ...]


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
    d_ranges=((0, 11999),),
    d_split_boundary=4095,
    m_ranges=((0, 4095),),
    m_split_boundary=1535,
    x_max_octal=0o377,
    y_max_octal=0o377,
    guarded_d=_DEFAULT_GUARDED_D,
    guarded_m=_DEFAULT_GUARDED_M,
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
    d_ranges=((0, 9999),),
    d_split_boundary=4095,
    m_ranges=((0, 4095),),
    m_split_boundary=1535,
    x_max_octal=0o377,
    y_max_octal=0o377,
    guarded_d=_DEFAULT_GUARDED_D,
    guarded_m=_DEFAULT_GUARDED_M,
)

PROFILE_SS2 = SeriesProfile(
    name="SS2",
    aliases=(
        "SS2",
        "14SS2",
        "DVP_SS2",
        "DVP_14SS2",
    ),
    d_ranges=((0, 4999),),
    d_split_boundary=4095,
    m_ranges=((0, 4095),),
    m_split_boundary=1535,
    x_max_octal=0o377,
    y_max_octal=0o377,
    guarded_d=_DEFAULT_GUARDED_D,
    guarded_m=_DEFAULT_GUARDED_M,
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
    d_ranges=((0, 4999),),
    d_split_boundary=4095,
    m_ranges=((0, 4095),),
    m_split_boundary=1535,
    x_max_octal=0o177,
    y_max_octal=0o177,
    guarded_d=_DEFAULT_GUARDED_D,
    guarded_m=_DEFAULT_GUARDED_M,
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
    d_ranges=((0, 599), (1000, 1311)),
    d_split_boundary=None,
    m_ranges=((0, 1279),),
    m_split_boundary=None,
    x_max_octal=0o177,
    y_max_octal=0o177,
    guarded_d=_DEFAULT_GUARDED_D,
    guarded_m=_DEFAULT_GUARDED_M,
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


def _intersect_ranges(
    r1: tuple[tuple[int, int], ...],
    r2: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    for s1, e1 in r1:
        for s2, e2 in r2:
            start = max(s1, s2)
            end = min(e1, e2)
            if start <= end:
                result.append((start, end))
    result.sort()
    return tuple(result)


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


def _resolve_split_boundary(
    b1: int | None,
    b2: int | None,
    valid_ranges: tuple[tuple[int, int], ...],
) -> int | None:
    candidate = min(b1, b2) if b1 is not None and b2 is not None else (b1 if b1 is not None else b2)
    if candidate is not None and any(start <= candidate <= end for start, end in valid_ranges):
        return candidate
    return None


def intersect_profiles(a: SeriesProfile, b: SeriesProfile) -> SeriesProfile:
    """Calculate the formal mathematical intersection of two SeriesProfiles.

    Computes:
    - Interval intersection of d_ranges and m_ranges
    - min() of x_max_octal and y_max_octal
    - Retains split boundary if present in either and within intersected range
    - Union of guarded_d and guarded_m
    """
    d_ranges = _intersect_ranges(a.d_ranges, b.d_ranges)
    m_ranges = _intersect_ranges(a.m_ranges, b.m_ranges)
    d_split_boundary = _resolve_split_boundary(a.d_split_boundary, b.d_split_boundary, d_ranges)
    m_split_boundary = _resolve_split_boundary(a.m_split_boundary, b.m_split_boundary, m_ranges)

    return SeriesProfile(
        name=f"{a.name}&{b.name}",
        aliases=(),
        d_ranges=d_ranges,
        d_split_boundary=d_split_boundary,
        m_ranges=m_ranges,
        m_split_boundary=m_split_boundary,
        x_max_octal=min(a.x_max_octal, b.x_max_octal),
        y_max_octal=min(a.y_max_octal, b.y_max_octal),
        guarded_d=_union_ranges(a.guarded_d, b.guarded_d),
        guarded_m=_union_ranges(a.guarded_m, b.guarded_m),
    )
