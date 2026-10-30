"""Public Delta DVP wire map shared by the driver, mock, and downstream tools.

The constants here describe the bench-faithful block layout the mock server
emulates: where each device starts on the Modbus wire, how many values it has,
which spans are addressable, which spans are read-only, and the PDU count
ceilings a real DVP enforces.  ``wire_for`` is re-exported from
:mod:`deltactl.addressing` so callers need only one import for the map.
"""

from __future__ import annotations

from .addressing import Device, wire_for

__all__ = [
    "D_COUNT",
    "D_SEGMENT_SPANS",
    "D_WIRE0",
    "M_COUNT",
    "M_SEGMENT_SPANS",
    "M_WIRE0",
    "PDU_CAPS",
    "READ_RANGES",
    "WRITE_GUARDS",
    "XY_COUNT",
    "X_WIRE0",
    "Y_WIRE0",
    "function_code_for",
    "wire_for",
]

# --------------------------------------------------------------------------- anchors

D_WIRE0 = 0x1000  # D0-D4095
M_WIRE0 = 0x0800  # M0-M1535
X_WIRE0 = 0x0400  # X0-X377 (read-only)
Y_WIRE0 = 0x0500  # Y0-Y377

D_COUNT = 12000  # D0-D11999
M_COUNT = 4096  # M0-M4095
XY_COUNT = 0o400  # X0/Y0-Y377

#: Modbus read function per device: holding registers (D), discrete inputs (X),
#: coils (M/Y).  The simulator context accepts the same codes for writes.
_FUNCTION_FOR: dict[Device, int] = {
    Device.D: 3,
    Device.X: 2,
    Device.M: 1,
    Device.Y: 1,
}

# --------------------------------------------------------------------------- spans

#: hr block spans as (start, stop, readonly) index ranges: the middle spans are
#: the bench's read-only system, scan, and fault registers.
D_SEGMENT_SPANS: tuple[tuple[int, int, bool], ...] = (
    (0, 1000, False),
    (1000, 1003, True),  # D1000-D1002
    (1003, 1010, False),
    (1010, 1013, True),  # D1010-D1012
    (1013, 1067, False),
    (1067, 1068, True),  # D1067
    (1068, 4096, False),
    (4096, 12000, False),
)

M_SEGMENT_SPANS: tuple[tuple[int, int, bool], ...] = (
    (0, 1000, False),
    (1000, 1004, True),  # M1000-M1003
    (1004, 1067, False),
    (1067, 1068, True),  # M1067
    (1068, 1536, False),
    (1536, 4096, False),
)

#: PDU count ceilings a real DVP enforces (FC03/FC16 vs the bit function codes).
PDU_CAPS: dict[int, int] = {1: 256, 2: 256, 3: 100, 15: 256, 16: 100}

#: Wire spans that may be read/written; anything else is address 02.
READ_RANGES: dict[int, tuple[tuple[int, int], ...]] = {
    1: ((M_WIRE0, 0x0DFF), (0xB000, 0xB9FF), (Y_WIRE0, 0x05FF)),
    2: ((X_WIRE0, 0x04FF),),
    3: ((D_WIRE0, 0x1FFF), (0x9000, 0xAEDF)),
    5: ((M_WIRE0, 0x0DFF), (0xB000, 0xB9FF), (Y_WIRE0, 0x05FF)),
    6: ((D_WIRE0, 0x1FFF), (0x9000, 0xAEDF)),
    15: ((M_WIRE0, 0x0DFF), (0xB000, 0xB9FF), (Y_WIRE0, 0x05FF)),
    16: ((D_WIRE0, 0x1FFF), (0x9000, 0xAEDF)),
}

#: Read-only write guards, in wire space, as inclusive spans; reads are unaffected.
WRITE_GUARDS: dict[int, tuple[tuple[int, int], ...]] = {
    5: ((0x0BE8, 0x0BEB), (0x0C2B, 0x0C2B), (X_WIRE0, 0x04FF)),
    15: ((0x0BE8, 0x0BEB), (0x0C2B, 0x0C2B), (X_WIRE0, 0x04FF)),
    6: ((0x13E8, 0x13EA), (0x13F2, 0x13F4), (0x142B, 0x142B)),
    16: ((0x13E8, 0x13EA), (0x13F2, 0x13F4), (0x142B, 0x142B)),
}


def function_code_for(area: Device) -> int:
    """Modbus function code that addresses ``area`` (D→03, X→02, M/Y→01)."""
    return _FUNCTION_FOR[area]
