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
    ModelDetectionError,
    ModelMismatchError,
    NotConnectedError,
    PollerError,
    ReadOnlyAddressError,
    UnknownModelCodeError,
)
from .mock import MockServer
from .planning import Tag
from .poller import Frame, TagPoller
from .transport import AsyncDeltaPLC

__version__ = "0.2.0"

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
    "Frame",
    "MockServer",
    "ModelDetectionError",
    "ModelMismatchError",
    "NotConnectedError",
    "PollerError",
    "ReadOnlyAddressError",
    "Tag",
    "TagPoller",
    "UnknownModelCodeError",
    "__version__",
]
