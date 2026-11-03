# tests/test_profiles.py
from types import MappingProxyType

import pytest

from deltactl.errors import DeltaValueError, UnknownModelCodeError
from deltactl.profiles import (
    DEFAULT_PROFILE,
    DEVICE_VIEWS,
    PROFILE_ES2_EX2,
    PROFILE_ES_EX_SS,
    PROFILE_SA_SX_SC,
    PROFILE_SE_EH3_SV2,
    PROFILE_SS2,
    Device,
    DeviceSpec,
    Segment,
    SeriesProfile,
    View,
    ViewSpec,
    decode_d1001,
    get_profile,
    intersect_profiles,
)


def _segments(profile: SeriesProfile, device: Device) -> tuple[Segment, ...]:
    return profile.devices[device].views[DEVICE_VIEWS[device][0]].segments


def test_canonical_profiles_exist():
    assert DEFAULT_PROFILE == PROFILE_SE_EH3_SV2
    assert _segments(PROFILE_SE_EH3_SV2, Device.D) == (
        Segment(0, 4095, 0x1000),
        Segment(4096, 11999, 0x9000),
    )
    assert _segments(PROFILE_ES2_EX2, Device.D) == (
        Segment(0, 4095, 0x1000),
        Segment(4096, 9999, 0x9000),
    )
    assert _segments(PROFILE_SS2, Device.D) == (
        Segment(0, 4095, 0x1000),
        Segment(4096, 4999, 0x9000),
    )
    assert _segments(PROFILE_SA_SX_SC, Device.D) == (
        Segment(0, 4095, 0x1000),
        Segment(4096, 4999, 0x9000),
    )
    assert _segments(PROFILE_SA_SX_SC, Device.X) == (Segment(0, 0o177, 0x0400),)
    assert _segments(PROFILE_ES_EX_SS, Device.D) == (
        Segment(0, 599, 0x1000),
        Segment(1000, 1311, 0x13E8),
    )
    assert _segments(PROFILE_ES_EX_SS, Device.M) == (Segment(0, 1279, 0x0800),)


def test_alias_normalization_and_hygiene():
    assert get_profile("SE") == PROFILE_SE_EH3_SV2
    assert get_profile("DVP-12SE") == PROFILE_SE_EH3_SV2
    assert get_profile("eh3") == PROFILE_SE_EH3_SV2
    assert get_profile("ES2") == PROFILE_ES2_EX2
    assert get_profile("12SA2") == PROFILE_ES2_EX2
    assert get_profile("SS2") == PROFILE_SS2
    assert get_profile("14SS2") == PROFILE_SS2
    assert get_profile("SA") == PROFILE_SA_SX_SC
    assert get_profile("SS") == PROFILE_ES_EX_SS
    assert get_profile("ES") == PROFILE_ES_EX_SS
    with pytest.raises(DeltaValueError):
        get_profile("UNKNOWN_SERIES")


@pytest.mark.parametrize(
    "upper_byte,expected_profile",
    [
        (0x7C, PROFILE_SE_EH3_SV2),
        (0x70, PROFILE_ES2_EX2),
        (0x72, PROFILE_SS2),
        (0x74, PROFILE_ES2_EX2),
        (0x76, PROFILE_ES2_EX2),
        (0x62, PROFILE_SE_EH3_SV2),
        (0x66, PROFILE_SE_EH3_SV2),
        (0x60, PROFILE_ES2_EX2),
        (0x64, PROFILE_ES2_EX2),
        (0x50, PROFILE_ES_EX_SS),
        (0x52, PROFILE_ES_EX_SS),
        (0x54, PROFILE_SA_SX_SC),
        (0x58, PROFILE_SA_SX_SC),
        (0x5A, PROFILE_SA_SX_SC),
    ],
)
def test_d1001_decoding_14_rows(upper_byte, expected_profile):
    raw = (upper_byte << 8) | 0x21
    profile, fw = decode_d1001(raw)
    assert profile == expected_profile
    assert fw == 0x21


def test_d1001_unknown_codes():
    for code in (0x00, 0x80, 0xFF):
        with pytest.raises(UnknownModelCodeError):
            decode_d1001(code << 8)


def test_intersect_profiles():
    inter = intersect_profiles(PROFILE_SE_EH3_SV2, PROFILE_SS2)
    assert _segments(inter, Device.D) == (
        Segment(0, 4095, 0x1000),
        Segment(4096, 4999, 0x9000),
    )
    assert _segments(inter, Device.X) == (Segment(0, 0o377, 0x0400),)
    incompat = intersect_profiles(PROFILE_SA_SX_SC, PROFILE_ES_EX_SS)
    assert _segments(incompat, Device.D) == (
        Segment(0, 599, 0x1000),
        Segment(1000, 1311, 0x13E8),
    )
    assert _segments(incompat, Device.X) == (Segment(0, 0o177, 0x0400),)


def test_default_profile_device_specs():
    p = PROFILE_SE_EH3_SV2
    d = p.devices[Device.D].views[View.WORD]
    assert d.function == 3 and d.writable is True
    assert [(s.first, s.last, s.wire, s.step) for s in d.segments] == [
        (0, 4095, 0x1000, 1),
        (4096, 11999, 0x9000, 1),
    ]
    assert d.guards == ((1000, 1002), (1010, 1012), (1067, 1067))
    m = p.devices[Device.M].views[View.BIT]
    assert [(s.first, s.last, s.wire) for s in m.segments] == [
        (0, 1535, 0x0800),
        (1536, 4095, 0xB000),
    ]
    assert m.guards == ((1000, 1003), (1067, 1067))
    assert p.devices[Device.X].views[View.BIT].writable is False
    assert p.devices[Device.Y].views[View.BIT].writable is True


def test_device_views_spelling_is_fixed():
    assert DEVICE_VIEWS[Device.D] == (View.WORD,)
    assert DEVICE_VIEWS[Device.M] == (View.BIT,)
    assert DEVICE_VIEWS[Device.X] == (View.BIT,)
    assert DEVICE_VIEWS[Device.Y] == (View.BIT,)


def test_intersection_matches_old_d_m_x_y():
    p = intersect_profiles(PROFILE_SE_EH3_SV2, PROFILE_ES_EX_SS)
    assert p.devices[Device.D].views[View.WORD].segments == (
        Segment(0, 599, 0x1000),
        Segment(1000, 1311, 0x13E8),
    )
    assert p.devices[Device.X].views[View.BIT].segments == (Segment(0, 0o177, 0x0400),)


def test_intersection_keeps_duplicate_key_segments():
    p = intersect_profiles(PROFILE_ES_EX_SS, PROFILE_SE_EH3_SV2)
    ranges = [(s.first, s.last) for s in p.devices[Device.D].views[View.WORD].segments]
    assert ranges == [(0, 599), (1000, 1311)]


def _stepped_profile(name: str, *, step: int, x_writable: bool) -> SeriesProfile:
    return SeriesProfile(
        name=name,
        aliases=(),
        devices=MappingProxyType(
            {
                Device.D: DeviceSpec(
                    MappingProxyType(
                        {
                            View.WORD: ViewSpec(
                                3, True, (Segment(0, 9, 0x1000, step=step),), ((1, 2),)
                            )
                        }
                    )
                ),
                Device.X: DeviceSpec(
                    MappingProxyType(
                        {View.BIT: ViewSpec(2, x_writable, (Segment(0, 0o7, 0x0400),))}
                    )
                ),
            }
        ),
    )


def test_intersection_uses_segment_wire_keys():
    p = intersect_profiles(
        _stepped_profile("a", step=2, x_writable=True),
        _stepped_profile("b", step=2, x_writable=False),
    )
    assert p.devices[Device.D].views[View.WORD].segments == (Segment(0, 9, 0x1000, step=2),)
    assert p.devices[Device.D].views[View.WORD].guards == ((1, 2),)
    assert p.devices[Device.X].views[View.BIT].writable is False


def _d_only_profile(name: str, first: int, last: int) -> SeriesProfile:
    """Synthetic profile with one D segment anchored like the real 0x1000 block."""
    return SeriesProfile(
        name=name,
        aliases=(),
        devices=MappingProxyType(
            {
                Device.D: DeviceSpec(
                    MappingProxyType(
                        {
                            View.WORD: ViewSpec(
                                3,
                                True,
                                (Segment(first, last, 0x1000 + first),),
                            )
                        }
                    )
                )
            }
        ),
    )


def test_empty_intersection_stays_empty():
    # Disjoint D index ranges sharing a wire-mapping key: no segment survives,
    # so the result must keep an empty devices mapping instead of resurrecting
    # a default or unioned range.
    left = _d_only_profile("left", 0, 9)
    right = _d_only_profile("right", 20, 29)
    for a, b in ((left, right), (right, left)):
        result = intersect_profiles(a, b)
        assert result.devices == {}
        assert Device.D not in result.devices


def test_canonical_profiles_stc_devices():
    p_es = PROFILE_ES_EX_SS
    assert p_es.devices[Device.S].views[View.BIT].segments == (Segment(0, 127, 0x0000),)
    assert p_es.devices[Device.T].views[View.BIT].segments == (Segment(0, 127, 0x0600),)
    assert p_es.devices[Device.T].views[View.WORD].segments == (
        Segment(0, 127, 0x0600, value_width=1, default_dtype="u16"),
    )
    assert p_es.devices[Device.C].views[View.BIT].segments == (
        Segment(0, 127, 0x0E00),
        Segment(235, 254, 0x0EEB),
    )
    assert p_es.devices[Device.C].views[View.WORD].segments == (
        Segment(0, 127, 0x0E00, value_width=1, default_dtype="u16"),
        Segment(235, 254, 0x0EEB, value_width=2, default_dtype="i32", coalesce=False),
    )

    for p in (PROFILE_SE_EH3_SV2, PROFILE_ES2_EX2, PROFILE_SS2, PROFILE_SA_SX_SC):
        assert p.devices[Device.S].views[View.BIT].segments == (Segment(0, 1023, 0x0000),)
        assert p.devices[Device.T].views[View.BIT].segments == (Segment(0, 255, 0x0600),)
        assert p.devices[Device.T].views[View.WORD].segments == (
            Segment(0, 255, 0x0600, value_width=1, default_dtype="u16"),
        )
        assert p.devices[Device.C].views[View.BIT].segments == (
            Segment(0, 199, 0x0E00),
            Segment(200, 254, 0x0EC8),
        )
        assert p.devices[Device.C].views[View.WORD].segments == (
            Segment(0, 199, 0x0E00, value_width=1, default_dtype="u16"),
            Segment(200, 254, 0x0EC8, value_width=2, default_dtype="i32", coalesce=False),
        )
        assert p.devices[Device.S].views[View.BIT].writable is False
        assert p.devices[Device.T].views[View.BIT].writable is False
        assert p.devices[Device.T].views[View.WORD].writable is False
        assert p.devices[Device.C].views[View.BIT].writable is False
        assert p.devices[Device.C].views[View.WORD].writable is False
