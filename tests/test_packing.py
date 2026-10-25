# tests/test_packing.py
import math
import pytest
from delta_plc.packing import (
    DType, decode_value, decode_values, encode_value, encode_values,
    get_bit, mask_u16, set_bit,
)
from delta_plc.errors import DeltaValueError

def test_u16_i16_roundtrip():
    assert encode_value(65535, DType.U16) == [0xFFFF]
    assert decode_value([0xFFFF], DType.I16) == -1
    assert mask_u16(-1) == 0xFFFF
    assert encode_values([1, 2], DType.U16) == [1, 2]

@pytest.mark.parametrize("dtype,value,regs", [
    (DType.I32, 0x12345678, [0x5678, 0x1234]),
    (DType.U32, 0xDEADBEEF, [0xBEEF, 0xDEAD]),
    (DType.F32, 12.5, [0x0000, 0x4148]),
    (DType.F32, -12.5, [0x0000, 0xC148]),
])
def test_cdab_encoding(dtype, value, regs):
    assert encode_value(value, dtype) == regs
    assert decode_value(regs, dtype) == pytest.approx(value)

@pytest.mark.parametrize("value,dtype", [
    (65536, DType.U16), (-1, DType.U16), (32768, DType.I16),
    (2**31, DType.I32), (-1, DType.U32), ("12", DType.F32),
])
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
