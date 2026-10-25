from delta_plc.errors import (
    AddressRangeError,
    DeltaAddressError,
    DeltaModbusException,
    DeltaPLCError,
    DeltaValueError,
    ReadOnlyAddressError,
)


def test_address_errors_share_base():
    assert issubclass(AddressRangeError, DeltaAddressError)
    assert issubclass(DeltaAddressError, DeltaPLCError)


def test_value_error_is_stdlib_value_error():
    assert issubclass(DeltaValueError, ValueError)
    assert issubclass(DeltaValueError, DeltaPLCError)


def test_modbus_exception_exposes_fields():
    err = DeltaModbusException(function_code=3, exception_code=2)
    assert (err.function_code, err.exception_code) == (3, 2)
    assert "function 3" in str(err) and "exception 2" in str(err)


def test_read_only_error_is_delta_plc_error():
    assert issubclass(ReadOnlyAddressError, DeltaPLCError)
