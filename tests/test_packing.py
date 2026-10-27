# tests/test_packing.py
import math

import pytest

from delta_plc.errors import DeltaValueError
from delta_plc.packing import (
    DType,
    decode_value,
    decode_values,
    encode_value,
    encode_values,
    get_bit,
    mask_u16,
    set_bit,
)


def test_u16_i16_roundtrip():
    assert encode_value(65535, DType.U16) == [0xFFFF]
    assert decode_value([0xFFFF], DType.I16) == -1
    assert mask_u16(-1) == 0xFFFF
    assert encode_values([1, 2], DType.U16) == [1, 2]


@pytest.mark.parametrize(
    "dtype,value,regs",
    [
        (DType.I32, 0x12345678, [0x5678, 0x1234]),
        (DType.U32, 0xDEADBEEF, [0xBEEF, 0xDEAD]),
        (DType.F32, 12.5, [0x0000, 0x4148]),
        (DType.F32, -12.5, [0x0000, 0xC148]),
    ],
)
def test_cdab_encoding(dtype, value, regs):
    assert encode_value(value, dtype) == regs
    assert decode_value(regs, dtype) == pytest.approx(value)


@pytest.mark.parametrize(
    "value,dtype",
    [
        (65536, DType.U16),
        (-1, DType.U16),
        (32768, DType.I16),
        (2**31, DType.I32),
        (-1, DType.U32),
        ("12", DType.F32),
    ],
)
def test_out_of_range_rejected(value, dtype):
    with pytest.raises(DeltaValueError):
        encode_value(value, dtype)


def test_short_register_sequence_rejected():
    with pytest.raises(DeltaValueError):
        decode_value([0x1234], DType.I32)


def test_bool_rejected_for_numeric_dtypes():
    for dtype in (DType.U16, DType.I16, DType.I32, DType.U32, DType.F32):
        with pytest.raises(DeltaValueError):
            encode_value(True, dtype)


def test_non_finite_f32_encodes():
    assert len(encode_value(math.nan, DType.F32)) == 2
    assert len(encode_value(math.inf, DType.F32)) == 2


def test_bit_helpers():
    word = 0b0001
    assert get_bit(word, 0) is True
    assert get_bit(word, 1) is False
    assert set_bit(word, 1, True) == 0b0011
    assert set_bit(0b1111, 3, False) == 0b0111
    with pytest.raises(DeltaValueError):
        get_bit(word, -1)
    with pytest.raises(DeltaValueError):
        get_bit(word, 16)
    with pytest.raises(DeltaValueError):
        set_bit(word, -1, True)
    with pytest.raises(DeltaValueError):
        set_bit(word, 16, True)


def test_decode_values_and_multiple():
    assert decode_values([1, 2, 3], DType.U16) == [1, 2, 3]
    f32_regs = encode_value(12.5, DType.F32) + encode_value(-12.5, DType.F32)
    decoded = decode_values(f32_regs, DType.F32)
    assert decoded == pytest.approx([12.5, -12.5])


def test_mask_u16_validation():
    assert mask_u16(0) == 0
    assert mask_u16(65535) == 65535
    assert mask_u16(-32768) == 32768
    with pytest.raises(DeltaValueError):
        mask_u16(65536)
    with pytest.raises(DeltaValueError):
        mask_u16(-32769)
    with pytest.raises(DeltaValueError):
        mask_u16("not an int")  # type: ignore[arg-type]


def test_decode_invalid_registers():
    with pytest.raises(DeltaValueError):
        decode_value([-1], DType.U16)
    with pytest.raises(DeltaValueError):
        decode_value([0x10000], DType.U16)
    with pytest.raises(DeltaValueError):
        decode_value(["not an int"], DType.U16)  # type: ignore[list-item]
    with pytest.raises(DeltaValueError):
        decode_value([True], DType.U16)  # type: ignore[list-item]


def test_bit_dtype_roundtrip():
    assert encode_value(True, DType.BIT) == [1]
    assert encode_value(False, DType.BIT) == [0]
    assert decode_value([1], DType.BIT) == 1
    assert decode_value([0], DType.BIT) == 0


def test_non_int_rejected_for_int_dtypes():
    with pytest.raises(DeltaValueError):
        encode_value("hello", DType.U16)  # type: ignore[arg-type]
    with pytest.raises(DeltaValueError):
        encode_value(None, DType.I32)  # type: ignore[arg-type]
