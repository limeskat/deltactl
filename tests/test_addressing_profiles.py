import pytest

from deltactl.addressing import parse_tag, validate_span
from deltactl.errors import AddressRangeError
from deltactl.profiles import (
    PROFILE_ES2_EX2,
    PROFILE_ES_EX_SS,
    PROFILE_SA_SX_SC,
    PROFILE_SE_EH3_SV2,
    PROFILE_SS2,
)


def test_bounds_all_profiles():
    # SE: D11999 valid, D12000 invalid
    assert parse_tag("D11999", profile=PROFILE_SE_EH3_SV2).index == 11999
    with pytest.raises(AddressRangeError):
        parse_tag("D12000", profile=PROFILE_SE_EH3_SV2)

    # ES2: D9999 valid, D10000 invalid
    assert parse_tag("D9999", profile=PROFILE_ES2_EX2).index == 9999
    with pytest.raises(AddressRangeError):
        parse_tag("D10000", profile=PROFILE_ES2_EX2)

    # SS2: D4999 valid, D5000 invalid
    assert parse_tag("D4999", profile=PROFILE_SS2).index == 4999
    with pytest.raises(AddressRangeError):
        parse_tag("D5000", profile=PROFILE_SS2)

    # SA_SX_SC: D4999 valid, D5000 invalid
    assert parse_tag("D4999", profile=PROFILE_SA_SX_SC).index == 4999
    with pytest.raises(AddressRangeError):
        parse_tag("D5000", profile=PROFILE_SA_SX_SC)


def test_es_ex_ss_interior_gap():
    # D599 valid, D600 invalid, D999 invalid, D1000 valid, D1311 valid, D1312 invalid
    assert parse_tag("D599", profile=PROFILE_ES_EX_SS).index == 599
    with pytest.raises(AddressRangeError):
        parse_tag("D600", profile=PROFILE_ES_EX_SS)
    with pytest.raises(AddressRangeError):
        parse_tag("D999", profile=PROFILE_ES_EX_SS)
    assert parse_tag("D1000", profile=PROFILE_ES_EX_SS).index == 1000
    assert parse_tag("D1311", profile=PROFILE_ES_EX_SS).index == 1311
    with pytest.raises(AddressRangeError):
        parse_tag("D1312", profile=PROFILE_ES_EX_SS)


def test_es_ex_ss_gap_spanning_rejected():
    tag = parse_tag("D599", profile=PROFILE_ES_EX_SS)
    # Reading 2 words from D599 crosses into gap D600
    with pytest.raises(AddressRangeError):
        validate_span(tag, count=2, write=False, profile=PROFILE_ES_EX_SS)


def test_octal_bounds_per_profile():
    # 256 pts
    assert parse_tag("X377", profile=PROFILE_SS2).index == 255
    with pytest.raises(AddressRangeError):
        parse_tag("X400", profile=PROFILE_SS2)
    # 128 pts
    assert parse_tag("X177", profile=PROFILE_SA_SX_SC).index == 127
    with pytest.raises(AddressRangeError):
        parse_tag("X200", profile=PROFILE_SA_SX_SC)
