"""Exception hierarchy for the Delta DVP PLC driver."""

__all__ = [
    "AddressRangeError",
    "AddressSyntaxError",
    "BlockBoundaryError",
    "DeltaAddressError",
    "DeltaConnectionError",
    "DeltaModbusException",
    "DeltaPLCError",
    "DeltaProtocolError",
    "DeltaTimeoutError",
    "DeltaValueError",
    "NotConnectedError",
    "PollerError",
    "ReadOnlyAddressError",
    "UnknownModelCodeError",
]


class DeltaPLCError(Exception):
    """Base class for every error raised by this driver."""


class DeltaAddressError(DeltaPLCError):
    """Base class for invalid Delta device addresses."""


class AddressSyntaxError(DeltaAddressError):
    """The address text does not match the supported Delta syntax."""


class AddressRangeError(DeltaAddressError):
    """The address or its span falls outside the device's valid range."""


class BlockBoundaryError(DeltaAddressError):
    """A single value would straddle a Delta memory-block boundary."""


class DeltaValueError(DeltaPLCError, ValueError):
    """A value is invalid for the requested Delta data type."""


class ReadOnlyAddressError(DeltaPLCError):
    """The address is guarded against writes by this driver."""


class NotConnectedError(DeltaPLCError):
    """An operation required a connection but the client is not connected."""


class DeltaConnectionError(DeltaPLCError):
    """The Modbus TCP connection could not be established or was lost."""


class DeltaTimeoutError(DeltaPLCError):
    """The PLC did not answer within the configured timeout."""


class DeltaProtocolError(DeltaPLCError):
    """The PLC returned a malformed, truncated, or unexpected response."""


class UnknownModelCodeError(DeltaProtocolError):
    """Raised when D1001 returns an upper byte not in the Delta model registry."""


class DeltaModbusException(DeltaProtocolError):
    """The PLC answered a request with a Modbus exception code."""

    def __init__(self, function_code: int, exception_code: int) -> None:
        self.function_code = function_code
        self.exception_code = exception_code
        super().__init__(f"Modbus function {function_code} returned exception {exception_code}")


class PollerError(DeltaPLCError):
    """The poller cannot honor its configuration or continue polling."""
