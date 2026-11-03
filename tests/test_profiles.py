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


def test_canonical_profiles_exist():
    assert DEFAULT_PROFILE == PROFILE_SE_EH3_SV2
    assert PROFILE_SE_EH3_SV2.d_ranges == ((0, 11999),)
    assert PROFILE_SE_EH3_SV2.d_split_boundary == 4095
    assert PROFILE_ES2_EX2.d_ranges == ((0, 9999),)
    assert PROFILE_SS2.d_ranges == ((0, 4999),)
    assert PROFILE_SS2.d_split_boundary == 4095
    assert PROFILE_SA_SX_SC.d_ranges == ((0, 4999),)
    assert PROFILE_SA_SX_SC.d_split_boundary == 4095
    assert PROFILE_SA_SX_SC.x_max_octal == 0o177
    assert PROFILE_ES_EX_SS.d_ranges == ((0, 599), (1000, 1311))
    assert PROFILE_ES_EX_SS.d_split_boundary is None
    assert PROFILE_ES_EX_SS.m_ranges == ((0, 1279),)
    assert PROFILE_ES_EX_SS.m_split_boundary is None


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
    assert inter.d_ranges == ((0, 4999),)
    assert inter.d_split_boundary == 4095
    assert inter.x_max_octal == 0o377
    incompat = intersect_profiles(PROFILE_SA_SX_SC, PROFILE_ES_EX_SS)
    assert incompat.d_ranges == ((0, 599), (1000, 1311))
    assert incompat.x_max_octal == 0o177


def test_default_profile_devices_from_flat_fields():
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
        d_ranges=((0, 9),),
        d_split_boundary=None,
        m_ranges=((0, 9),),
        m_split_boundary=None,
        x_max_octal=0o7,
        y_max_octal=0o7,
        guarded_d=((1, 2),),
        guarded_m=(),
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


def test_intersection_uses_segments_not_flat_fields():
    p = intersect_profiles(
        _stepped_profile("a", step=2, x_writable=True),
        _stepped_profile("b", step=2, x_writable=False),
    )
    assert p.devices[Device.D].views[View.WORD].segments == (Segment(0, 9, 0x1000, step=2),)
    assert p.devices[Device.D].views[View.WORD].guards == ((1, 2),)
    assert p.devices[Device.X].views[View.BIT].writable is False
