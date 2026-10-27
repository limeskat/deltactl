# tests/test_profiles.py
import pytest

from deltactl.errors import DeltaValueError, UnknownModelCodeError
from deltactl.profiles import (
    DEFAULT_PROFILE,
    PROFILE_ES2_EX2,
    PROFILE_ES_EX_SS,
    PROFILE_SA_SX_SC,
    PROFILE_SE_EH3_SV2,
    PROFILE_SS2,
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
