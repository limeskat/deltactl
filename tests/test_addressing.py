# tests/test_addressing.py
from types import MappingProxyType

import pytest

from deltactl.addressing import (
    _extract_address,
    _resolve_view,
    expand_tags,
    normalize_tag,
    parse_raw,
    parse_tag,
    raw_type_for,
    validate_span,
    wire_for,
)
from deltactl.errors import (
    AddressRangeError,
    AddressSyntaxError,
    BlockBoundaryError,
    DeltaValueError,
    ReadOnlyAddressError,
)
from deltactl.packing import DType
from deltactl.planning import make_tag
from deltactl.profiles import (
    DEVICE_VIEWS,
    Device,
    DeviceSpec,
    Segment,
    SeriesProfile,
    View,
    ViewSpec,
    intersect_profiles,
)


@pytest.mark.parametrize(
    "tag,wire,block",
    [
        ("D0", 0x1000, 0),
        ("D4095", 0x1FFF, 0),
        ("D4096", 0x9000, 1),
        ("D11999", 0xAEDF, 1),
        ("M0", 0x0800, 0),
        ("M1535", 0x0DFF, 0),
        ("M1536", 0xB000, 1),
        ("M4095", 0xB9FF, 1),
        ("X0", 0x0400, 0),
        ("X377", 0x04FF, 0),
        ("Y0", 0x0500, 0),
        ("Y377", 0x05FF, 0),
    ],
)
def test_wire_map(tag, wire, block):
    parsed = parse_tag(tag)
    assert (parsed.address.wire, parsed.address.block) == (wire, block)


def test_octal_and_normalization():
    assert parse_tag("X10").address.index == 8
    assert parse_tag("x10").display == "X10"
    assert parse_tag("D0100").display == "D100"
    assert parse_tag(" d100:f32 ").display == "D100:f32"
    assert normalize_tag("D0100:F32") == "D100:f32"


@pytest.mark.parametrize(
    "bad",
    [
        "D",
        "D-1",
        "D1.16",
        "M1.2",
        "X8",
        "Y9",
        "D100:u8",
        "D100.3:f32",
        "D 100",
        "D100abc",
        "D12000",
        "M4096",
        "X400",
    ],
)
def test_invalid_tags(bad):
    with pytest.raises((AddressSyntaxError, AddressRangeError)):
        parse_tag(bad)


def test_full_span_guards_and_bounds():
    with pytest.raises(ReadOnlyAddressError):
        validate_span(parse_tag("D999").address, 2, write=True)
    with pytest.raises(ReadOnlyAddressError):
        validate_span(parse_tag("D1000.3").address, 1, write=True)
    with pytest.raises(ReadOnlyAddressError):
        validate_span(parse_tag("M999").address, 2, write=True)
    with pytest.raises(ReadOnlyAddressError):
        validate_span(parse_tag("X0").address, 1, write=True)
    with pytest.raises(AddressRangeError):
        validate_span(parse_tag("D11999").address, 2, write=False)
    with pytest.raises(BlockBoundaryError):
        validate_span(parse_tag("D4095").address, 2, write=False)
    validate_span(parse_tag("D4094").address, 2, write=False)
    validate_span(parse_tag("D4095").address, 2, write=False, allow_block_crossing=True)
    validate_span(parse_tag("Y377").address, 1, write=True)


@pytest.mark.parametrize(
    "raw,raw_type,display",
    [
        (0x1000, "d", "D0"),
        (0x9000, "d", "D4096"),
        (0x0800, "m", "M0"),
        (0xB000, "m", "M1536"),
        (0x0400, "x", "X0"),
        (0x04FF, "x", "X377"),
        (0x0500, "y", "Y0"),
        (0x05FF, "y", "Y377"),
    ],
)
def test_raw_decoding(raw, raw_type, display):
    assert normalize_tag(parse_raw(raw, raw_type).display) == display


def test_raw_outside_blocks_rejected():
    with pytest.raises(AddressRangeError):
        parse_raw(0x2000, "d")
    with pytest.raises(AddressRangeError):
        parse_raw(0x0600, "y")


def test_range_expansion_dedupe_and_cap():
    assert expand_tags(["D100-D102"]) == ["D100", "D101", "D102"]
    assert expand_tags(["D100", "d100", "D101"]) == ["D100", "D101"]
    assert expand_tags(["D100-D101:f32"]) == ["D100:f32", "D101:f32"]
    with pytest.raises(ValueError):
        expand_tags(["D100-M101"])
    with pytest.raises(ValueError):
        expand_tags(["D0-D1024"])


@pytest.mark.parametrize(
    "text,name",
    [
        ("S0", "S0"),
        ("s1023", "S1023"),
        ("T0.c", "T0.c"),
        ("t255.v", "T255.v"),
        ("C0.c", "C0.c"),
        ("C200.v", "C200.v"),
    ],
)
def test_new_tags_parse(text, name):
    assert parse_tag(text).display == name


@pytest.mark.parametrize(
    "text",
    [
        "T0",
        "C0",
        "T0.3",
        "S0.c",
        "D100.c",
        "M0.v",
        "C200.v:u16",
        "C0.v:u32",
        "C200.v:f32",
        "T0.c:u32",
    ],
)
def test_new_tags_reject(text):
    with pytest.raises(AddressSyntaxError):
        parse_tag(text)


@pytest.mark.parametrize("tag", ["T0", "C0"])
def test_bare_multiview_tags_suggest_c_and_v(tag):
    with pytest.raises(AddressSyntaxError) as exc_info:
        parse_tag(tag)
    msg = str(exc_info.value)
    assert ".c" in msg and ".v" in msg
    assert ".b" not in msg and ".w" not in msg


def test_c200_defaults_to_i32():
    tag = make_tag("C200.v")
    assert tag.dtype is DType.I32 and tag.width == 2 and tag.address.wire == 0x0EC8


def test_t0_value_is_u16():
    tag = make_tag("T0.v")
    assert tag.dtype is DType.U16 and tag.address.wire == 0x0600


def test_new_device_views_are_fixed():
    assert DEVICE_VIEWS[Device.S] == (View.BIT,)
    assert DEVICE_VIEWS[Device.T] == (View.BIT, View.WORD)
    assert DEVICE_VIEWS[Device.C] == (View.BIT, View.WORD)


def test_span_range_message_uses_index_count():
    addr = parse_tag("C254.v").address
    with pytest.raises(AddressRangeError, match=r"\[254, 256\)"):
        validate_span(addr, 4)


@pytest.mark.parametrize("tag_text", ["S0", "T0.c", "T0.v", "C0.c", "C0.v", "C200.v"])
def test_stc_writes_rejected_as_read_only(tag_text):
    addr = parse_tag(tag_text).address
    with pytest.raises(ReadOnlyAddressError):
        validate_span(addr, addr.segment.value_width, write=True)


def test_make_tag_stc_fixed_width_rules():
    assert make_tag("C200.v", dtype=DType.U32).dtype is DType.U32
    assert make_tag("T0.v", dtype=DType.I16).dtype is DType.I16
    assert make_tag("C0.v", dtype=DType.I16).dtype is DType.I16

    assert make_tag("C200.v", default_dtype=DType.U16).dtype is DType.I32
    assert make_tag("T0.v", default_dtype=DType.I32).dtype is DType.U16

    with pytest.raises(DeltaValueError):
        make_tag("C200.v", dtype=DType.U16)
    with pytest.raises(DeltaValueError):
        make_tag("C200.v", dtype=DType.F32)
    with pytest.raises(DeltaValueError):
        make_tag("T0.v", dtype=DType.U32)
    with pytest.raises(DeltaValueError):
        make_tag("T0.v", dtype=DType.F32)


@pytest.mark.parametrize(
    "raw,raw_type,name",
    [
        ("0x0000", "s", "S0"),
        ("0x0600", "tc", "T0.c"),
        ("0x0600", "tv", "T0.v"),
        ("0x0E00", "cc", "C0.c"),
        ("0x0EC8", "cv", "C200.v"),
        ("0x0EC9", "cv", "C201.v"),
    ],
)
def test_parse_raw_new_types(raw, raw_type, name):
    assert parse_raw(raw, raw_type).display == name


def test_expand_view_ranges():
    assert expand_tags(["T0.c-T2.c"]) == ["T0.c", "T1.c", "T2.c"]
    assert expand_tags(["C200.v-C202.v"]) == ["C200.v", "C201.v", "C202.v"]


def test_expand_rejects_mixed_width_range():
    with pytest.raises(ValueError, match="width"):
        expand_tags(["C199.v-C200.v"])


def test_raw_type_for():
    for tag_text, expected_raw_type in [
        ("D0", "d"),
        ("M0", "m"),
        ("X0", "x"),
        ("Y0", "y"),
        ("S0", "s"),
        ("T0.c", "tc"),
        ("T0.v", "tv"),
        ("C0.c", "cc"),
        ("C200.v", "cv"),
    ]:
        tag = parse_tag(tag_text)
        assert raw_type_for(tag) == expected_raw_type
        assert raw_type_for(tag.address) == expected_raw_type


def test_expand_view_range_trailing_dtype():
    assert expand_tags(["T0.v-T2.v:u16"]) == ["T0.v:u16", "T1.v:u16", "T2.v:u16"]
    with pytest.raises(ValueError):
        expand_tags(["T0.c-T2.c:u16"])


def test_expand_rejects_numeric_members_in_endpoints():
    with pytest.raises(ValueError):
        expand_tags(["D100.0-D102.0"])


async def test_validate_planned_groups_new_types():
    from deltactl.planning import ReadGroup
    from deltactl.transport import AsyncDeltaPLC

    plc = AsyncDeltaPLC("127.0.0.1")
    # Valid groups with new types
    plc._validate_planned_groups([ReadGroup(3, 0x0600, 1, (make_tag("T0.v"),))])
    plc._validate_planned_groups([ReadGroup(1, 0x0600, 1, (make_tag("T0.c"),))])
    plc._validate_planned_groups([ReadGroup(3, 0x0EC8, 2, (make_tag("C200.v"),))])
    plc._validate_planned_groups([ReadGroup(1, 0x0000, 1, (make_tag("S0"),))])
    # Empty tags group skips re-parse
    plc._validate_planned_groups([ReadGroup(3, 0x0600, 1, ())])

    # Out-of-bounds start fails validation
    with pytest.raises(AddressRangeError):
        plc._validate_planned_groups([ReadGroup(3, 0x0750, 1, (make_tag("T0.v"),))])


def test_parsed_tag_properties():
    tag = parse_tag("D100.3")
    assert tag.device is Device.D
    assert tag.index == 100
    assert tag.wire == 0x1000 + 100
    assert tag.block == 0
    assert tag.bit == 3
    assert tag.view is View.WORD
    assert tag.function == 3
    assert tag.value_width == 1
    assert tag.default_dtype is None
    assert tag.coalesce is True

    s_tag = parse_tag("S0")
    assert s_tag.view is View.BIT
    assert s_tag.function == 1

    c_tag = parse_tag("C200.v")
    assert c_tag.default_dtype == "i32"
    assert c_tag.coalesce is False


def test_resolve_view_errors():
    with pytest.raises(ValueError, match="unknown device"):
        _resolve_view("INVALID", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown view"):
        _resolve_view(Device.T, "invalid")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no word view"):
        _resolve_view(Device.S, View.WORD)


def test_extract_address_type_error():
    with pytest.raises(TypeError, match="expected Address or ParsedTag"):
        _extract_address("not an address")  # type: ignore[arg-type]


def test_parse_raw_errors():
    with pytest.raises(ValueError, match="unknown raw type"):
        parse_raw(0x1000, "invalid_raw_type")


def test_expand_tags_validation_errors():
    with pytest.raises(ValueError, match="reversed"):
        expand_tags(["D10-D5"])
    with pytest.raises(ValueError, match="multiple devices"):
        expand_tags(["D0-M10"])
    with pytest.raises(ValueError, match="multiple views"):
        expand_tags(["T0.c-T2.v"])
    with pytest.raises(ValueError, match="endpoints must not contain dtype"):
        expand_tags(["D0:u16-D10"])
    with pytest.raises(ValueError, match="both endpoints must be valid tags"):
        expand_tags(["D99999-D100000"])
    with pytest.raises(ValueError, match="f32 is not allowed"):
        expand_tags(["T0.v-T2.v:f32"])
    with pytest.raises(ValueError, match="does not match"):
        expand_tags(["T0.v-T2.v:i32"])
    with pytest.raises(ValueError, match="limit"):
        expand_tags(["D0-D200"], limit=5)


def test_validate_span_errors():
    tag = parse_tag("D0")
    with pytest.raises(ValueError, match="span width or count must be specified"):
        validate_span(tag, width=None, count=None)
    with pytest.raises(ValueError, match="span width must be at least 1"):
        validate_span(tag, width=0)
    c32_tag = parse_tag("C200.v")
    with pytest.raises(ValueError, match="does not divide evenly"):
        validate_span(c32_tag, count=3)


def test_locate_missing_device_or_view_raises_address_range_error():
    empty_prof = SeriesProfile(name="empty", aliases=(), devices=MappingProxyType({}))
    with pytest.raises(AddressRangeError, match="D word is not available on profile empty"):
        wire_for(Device.D, 0, profile=empty_prof)
    with pytest.raises(AddressRangeError, match="D word is not available on profile empty"):
        parse_tag("D0", profile=empty_prof)

    left = SeriesProfile(
        name="left",
        aliases=(),
        devices=MappingProxyType(
            {
                Device.D: DeviceSpec(
                    MappingProxyType({View.WORD: ViewSpec(3, True, (Segment(0, 9, 0x1000),))})
                )
            }
        ),
    )
    right = SeriesProfile(
        name="right",
        aliases=(),
        devices=MappingProxyType(
            {
                Device.D: DeviceSpec(
                    MappingProxyType({View.WORD: ViewSpec(3, True, (Segment(20, 29, 0x1020),))})
                )
            }
        ),
    )
    inter = intersect_profiles(left, right)
    assert Device.D not in inter.devices
    with pytest.raises(AddressRangeError, match="D word is not available on profile"):
        wire_for(Device.D, 0, profile=inter)
    with pytest.raises(AddressRangeError, match="D word is not available on profile"):
        parse_tag("D0", profile=inter)

    bit_only = SeriesProfile(
        name="bit_only",
        aliases=(),
        devices=MappingProxyType(
            {
                Device.T: DeviceSpec(
                    MappingProxyType({View.BIT: ViewSpec(1, False, (Segment(0, 127, 0x0600),))})
                )
            }
        ),
    )
    with pytest.raises(AddressRangeError, match="T word is not available on profile bit_only"):
        wire_for(Device.T, 0, view=View.WORD, profile=bit_only)
    with pytest.raises(AddressRangeError, match="T word is not available on profile bit_only"):
        parse_tag("T0.v", profile=bit_only)
