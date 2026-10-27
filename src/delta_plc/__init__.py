"""Delta DVP PLC driver and CLI."""

from .client import DeltaPLC
from .errors import (
    AddressRangeError,
    AddressSyntaxError,
    BlockBoundaryError,
    DeltaAddressError,
    DeltaConnectionError,
    DeltaModbusException,
    DeltaPLCError,
    DeltaProtocolError,
    DeltaTimeoutError,
    DeltaValueError,
    NotConnectedError,
    PollerError,
    ReadOnlyAddressError,
)
from .transport import AsyncDeltaPLC

__version__ = "0.1.0"

__all__ = [
    "AddressRangeError",
    "AddressSyntaxError",
    "AsyncDeltaPLC",
    "BlockBoundaryError",
    "DeltaAddressError",
    "DeltaConnectionError",
    "DeltaModbusException",
    "DeltaPLC",
    "DeltaPLCError",
    "DeltaProtocolError",
    "DeltaTimeoutError",
    "DeltaValueError",
    "NotConnectedError",
    "PollerError",
    "ReadOnlyAddressError",
    "__version__",
]