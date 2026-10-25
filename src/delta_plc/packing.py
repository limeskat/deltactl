"""Encode and decode PLC values to and from 16-bit Modbus registers.

32-bit values use Delta's CDAB word order, which pymodbus expresses as
``word_order="little"`` (low word first, e.g. ``12.5`` -> ``[0x0000, 0x4148]``).
16-bit values are packed big-endian as a single register.
"""

from __future__ import annotations

import struct
from enum import Enum
from typing import Sequence

from pymodbus.client.mixin import ModbusClientMixin

from .errors import DeltaValueError

__all__ = [
    "DType",
    "decode_value",
    "decode_values",
    "encode_value",
    "encode_values",
    "get_bit",
    "mask_u16",
    "set_bit",
]


class DType(str, Enum):
    """A Delta PLC data type and its register width."""

    U16 = "u16"
    I16 = "i16"
    I32 = "i32"
    U32 = "u32"
    F32 = "f32"
    BIT = "bit"

    @property
    def width(self) -> int:
        """Number of 16-bit registers occupied by one value."""
        return 2 if self in (DType.I32, DType.U32, DType.F32) else 1


_RANGES: dict[DType, tuple[int, int]] = {
    DType.U16: (0, 0xFFFF),
    DType.I16: (-0x8000, 0x7FFF),
    DType.I32: (-(2**31), 2**31 - 1),
    DType.U32: (0, 2**32 - 1),
}

_MODBUS_DTYPES: dict[DType, ModbusClientMixin.DATATYPE] = {
    DType.I32: ModbusClientMixin.DATATYPE.INT32,
    DType.U32: ModbusClientMixin.DATATYPE.UINT32,
    DType.F32: ModbusClientMixin.DATATYPE.FLOAT32,
}

_PACK_ERRORS = (TypeError, struct.error, ValueError, OverflowError)


def encode_value(value: int | float | bool, dtype: DType) -> list[int]:
    """Encode a single Python value into registers for *dtype*."""
    dtype = DType(dtype)
    if isinstance(value, bool) and dtype is not DType.BIT:
        raise DeltaValueError(f"bool is not a valid {dtype.value} value")
    if dtype is DType.F32:
        if not isinstance(value, (int, float)):
            raise DeltaValueError(f"{value!r} is not a valid f32 value")
        try:
            return ModbusClientMixin.convert_to_registers(
                float(value), _MODBUS_DTYPES[dtype], word_order="little"
            )
        except _PACK_ERRORS as exc:
            raise DeltaValueError(f"cannot encode {value!r} as f32") from exc
    if not isinstance(value, int):
        raise DeltaValueError(f"{value!r} is not a valid {dtype.value} value")
    low, high = _RANGES.get(dtype, (0, 0xFFFF))
    if not low <= value <= high:
        raise DeltaValueError(f"{value!r} out of range for {dtype.value}")
    if dtype in _MODBUS_DTYPES:
        try:
            return ModbusClientMixin.convert_to_registers(
                value, _MODBUS_DTYPES[dtype], word_order="little"
            )
        except _PACK_ERRORS as exc:
            raise DeltaValueError(f"cannot encode {value!r} as {dtype.value}") from exc
    # 16-bit dtypes: one big-endian register (I16 as two's-complement low word).
    return [value & 0xFFFF]


def decode_value(registers: Sequence[int], dtype: DType) -> int | float:
    """Decode a register sequence of exactly ``dtype.width`` registers."""
    dtype = DType(dtype)
    regs = list(registers)
    if len(regs) != dtype.width:
        raise DeltaValueError(
            f"{dtype.value} needs {dtype.width} registers, got {len(regs)}"
        )
    if dtype not in _MODBUS_DTYPES:
        word = regs[0]
        if not isinstance(word, int) or isinstance(word, bool) or not 0 <= word <= 0xFFFF:
            raise DeltaValueError(f"{word!r} is not a valid register value")
        fmt = ">H" if dtype in (DType.U16, DType.BIT) else ">h"
        return struct.unpack(fmt, struct.pack(">H", word))[0]
    try:
        return ModbusClientMixin.convert_from_registers(
            regs, _MODBUS_DTYPES[dtype], word_order="little"
        )
    except _PACK_ERRORS as exc:
        raise DeltaValueError(f"cannot decode {regs!r} as {dtype.value}") from exc


def encode_values(values: Sequence[int | float | bool], dtype: DType) -> list[int]:
    """Encode a sequence of values into one flat register list."""
    return [reg for value in values for reg in encode_value(value, dtype)]


def decode_values(registers: Sequence[int], dtype: DType) -> list[int | float]:
    """Decode a flat register list into a list of values for *dtype*."""
    width = dtype.width
    return [
        decode_value(registers[start : start + width], dtype)
        for start in range(0, len(registers), width)
    ]


def mask_u16(value: int) -> int:
    """Clamp *value* into the 16-bit register space (``-32768..65535``)."""
    if not isinstance(value, int) or not -32768 <= value <= 65535:
        raise DeltaValueError(f"{value!r} out of range for a 16-bit register")
    return value & 0xFFFF


def get_bit(word: int, bit: int) -> bool:
    """Return the value of *bit* (0-15) of *word* as a real ``bool``."""
    if not 0 <= bit <= 15:
        raise DeltaValueError(f"bit {bit!r} out of range 0..15")
    return bool((word >> bit) & 1)


def set_bit(word: int, bit: int, value: bool) -> int:
    """Return *word* with *bit* (0-15) set to *value*, masked to 16 bits."""
    if not 0 <= bit <= 15:
        raise DeltaValueError(f"bit {bit!r} out of range 0..15")
    if value:
        return (word | (1 << bit)) & 0xFFFF
    return (word & ~(1 << bit)) & 0xFFFF
