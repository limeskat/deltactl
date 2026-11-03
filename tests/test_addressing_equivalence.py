"""Differential equivalence harness: pin D/M/X/Y addressing to v0.2.0.

Every expectation in this module is a transcription of the v0.2.0 flat
`SeriesProfile` fields plus the arithmetic the driver used at that tag.  It is
deliberately independent of `src/deltactl/`: do not import helpers from the
implementation or "fix" an expectation to match new behaviour.  A change here
means the refactor changed observable D/M/X/Y behaviour.
"""

import pytest

from deltactl.addressing import Device, block_for, parse_tag, validate_span, wire_for
from deltactl.errors import AddressRangeError, BlockBoundaryError, ReadOnlyAddressError
from deltactl.profiles import (
    PROFILE_ES2_EX2,
    PROFILE_ES_EX_SS,
    PROFILE_SA_SX_SC,
    PROFILE_SE_EH3_SV2,
    PROFILE_SS2,
)

PROFILES = [
    PROFILE_SE_EH3_SV2,
    PROFILE_ES2_EX2,
    PROFILE_SS2,
    PROFILE_SA_SX_SC,
    PROFILE_ES_EX_SS,
]

# v0.2.0 flat fields, copied verbatim from profiles.py at tag v0.2.0.
_G = ((1000, 1002), (1010, 1012), (1067, 1067))
_GM = ((1000, 1003), (1067, 1067))
LEGACY = {
    "SE_EH3_SV2": {
        "d": ((0, 11999),),
        "dsplit": 4095,
        "m": ((0, 4095),),
        "msplit": 1535,
        "x": 0o377,
        "y": 0o377,
        "gd": _G,
        "gm": _GM,
    },
    "ES2_EX2": {
        "d": ((0, 9999),),
        "dsplit": 4095,
        "m": ((0, 4095),),
        "msplit": 1535,
        "x": 0o377,
        "y": 0o377,
        "gd": _G,
        "gm": _GM,
    },
    "SS2": {
        "d": ((0, 4999),),
        "dsplit": 4095,
        "m": ((0, 4095),),
        "msplit": 1535,
        "x": 0o377,
        "y": 0o377,
        "gd": _G,
        "gm": _GM,
    },
    "SA_SX_SC": {
        "d": ((0, 4999),),
        "dsplit": 4095,
        "m": ((0, 4095),),
        "msplit": 1535,
        "x": 0o177,
        "y": 0o177,
        "gd": _G,
        "gm": _GM,
    },
    "ES_EX_SS": {
        "d": ((0, 599), (1000, 1311)),
        "dsplit": None,
        "m": ((0, 1279),),
        "msplit": None,
        "x": 0o177,
        "y": 0o177,
        "gd": _G,
        "gm": _GM,
    },
}


def _legacy_wire(row, device, index):
    if device is Device.D:
        if row["dsplit"] is not None and index > row["dsplit"]:
            return 0x9000 + (index - (row["dsplit"] + 1))
        return 0x1000 + index
    if device is Device.M:
        if row["msplit"] is not None and index > row["msplit"]:
            return 0xB000 + (index - (row["msplit"] + 1))
        return 0x0800 + index
    return (0x0400 if device is Device.X else 0x0500) + index


def _legacy_outcome(row, device, index, width, write):
    ranges = row["d"] if device is Device.D else row["m"]
    if device in (Device.X, Device.Y):
        limit = row["x"] if device is Device.X else row["y"]
        if not (0 <= index and index + width - 1 <= limit):
            return "range"
        if write and device is Device.X:
            return "read_only"
        return "ok"
    if not any(lo <= index and index + width - 1 <= hi for lo, hi in ranges):
        return "range"
    split = row["dsplit"] if device is Device.D else row["msplit"]
    if split is not None and index <= split < index + width - 1:
        return "block_boundary"
    if write:
        guards = row["gd"] if device is Device.D else row["gm"]
        end = index + width - 1
        if any(index <= hi and end >= lo for lo, hi in guards):
            return "read_only"
    return "ok"


def _tag(device, index):
    if device in (Device.X, Device.Y):
        return f"{device.value}{index:o}"
    return f"{device.value}{index}"


def _limit(row, device):
    if device is Device.D:
        return row["d"][-1][1]
    if device is Device.M:
        return row["m"][-1][1]
    return row["x"] if device is Device.X else row["y"]


def _legacy_in_range(row, device, index):
    """True when v0.2.0 accepted ``index`` for ``device`` on this profile."""
    if device is Device.D:
        return any(lo <= index <= hi for lo, hi in row["d"])
    if device is Device.M:
        return any(lo <= index <= hi for lo, hi in row["m"])
    return 0 <= index <= _limit(row, device)


def _actual_outcome(profile, device, index, width, write):
    """Outcome class from the live implementation, or "range" if unaddressable."""
    try:
        addr = parse_tag(_tag(device, index), profile=profile)
    except AddressRangeError:
        return "range"
    try:
        validate_span(addr, width, write=write, profile=profile)
    except ReadOnlyAddressError:
        return "read_only"
    except BlockBoundaryError:
        return "block_boundary"
    except AddressRangeError:
        return "range"
    return "ok"


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.name)
@pytest.mark.parametrize("device", [Device.D, Device.M, Device.X, Device.Y])
def test_wire_and_block_match_v020(profile, device):
    row = LEGACY[profile.name]
    for index in range(_limit(row, device) + 1):
        # Non-contiguous D ranges (ES_EX_SS) leave index gaps that v0.2.0
        # rejected; keep pinning that rejection instead of skipping silently.
        if not _legacy_in_range(row, device, index):
            with pytest.raises(AddressRangeError):
                wire_for(device, index, profile=profile)
            with pytest.raises(AddressRangeError):
                block_for(device, index, profile=profile)
            continue
        assert wire_for(device, index, profile=profile) == _legacy_wire(row, device, index)
        expected_block = 0
        if device is Device.D and row["dsplit"] is not None and index > row["dsplit"]:
            expected_block = 1
        if device is Device.M and row["msplit"] is not None and index > row["msplit"]:
            expected_block = 1
        assert block_for(device, index, profile=profile) == expected_block


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.name)
@pytest.mark.parametrize("device", [Device.D, Device.M, Device.X, Device.Y])
@pytest.mark.parametrize("width", [1, 2])
@pytest.mark.parametrize("write", [False, True])
def test_span_outcomes_match_v020(profile, device, width, write):
    row = LEGACY[profile.name]
    for index in range(_limit(row, device) + 1):
        expected = _legacy_outcome(row, device, index, width, write)
        assert _actual_outcome(profile, device, index, width, write) == expected, (
            profile.name,
            device,
            index,
            width,
            write,
        )
