# tests/test_addressing.py
import pytest

from deltactl.addressing import (
    expand_tags,
    normalize_tag,
    parse_raw,
    parse_tag,
    validate_span,
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
    View,
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
