import pytest
from delta_plc.mock import MockServer
from delta_plc.transport import AsyncDeltaPLC
from delta_plc.errors import (
    DeltaTimeoutError, DeltaValueError, ReadOnlyAddressError,
)

@pytest.fixture(scope="module")
def mock():
    server = MockServer(port=0)
    server.start()
    yield server
    server.stop()

@pytest.fixture
async def plc(mock):
    client = AsyncDeltaPLC("127.0.0.1", port=mock.address[1], timeout=1)
    await client.connect()
    yield client
    await client.close()

async def test_scalar_and_bulk_registers(plc):
    await plc.write("D9000", 42)
    assert await plc.read("D9000") == 42
    await plc.write("D9000", [1, 2, 3])
    assert await plc.read("D9000", count=3) == [1, 2, 3]
    await plc.write_f32("D9010", -12.5)
    assert await plc.write_f32("D9010", -12.5) is None
    assert await plc.read_f32("D9010") == pytest.approx(-12.5)

async def test_bits_and_y_outputs(plc):
    await plc.write("Y0", True)
    assert await plc.read("Y0") is True
    await plc.write_bits("M900", [True, False, True])
    assert await plc.read_bits("M900", count=3) == [True, False, True]
    await plc.write("D9020.1", True)
    await plc.write("D9020.3", True)
    assert await plc.read("D9020") == 0b1010
    await plc.write("D9020.1", False)
    assert await plc.read("D9020") == 0b1000

@pytest.mark.parametrize("call", [
    lambda p: p.write("X0", True),
    lambda p: p.write("D1000", 1),
    lambda p: p.write("D999", [1, 2]),
    lambda p: p.write_f32("D999", 1.0),
    lambda p: p.write("D1000.3", True),
    lambda p: p.write("M1067", True),
    lambda p: p.write_bits("M999", [True, True]),
    lambda p: p.write("D1012", 1),
])
async def test_guards_send_no_frame(plc, mock, call):
    before = mock.stats.requests
    with pytest.raises(ReadOnlyAddressError):
        await call(plc)
    assert mock.stats.requests == before

async def test_32bit_write_cannot_cross_split(plc, mock):
    from delta_plc.errors import BlockBoundaryError
    before = mock.stats.requests
    with pytest.raises(BlockBoundaryError):
        await plc.write_f32("D4095", 1.0)
    assert mock.stats.requests == before

async def test_untyped_d_bool_rejected(plc, mock):
    before = mock.stats.requests
    with pytest.raises(DeltaValueError):
        await plc.write("D9000", True)
    with pytest.raises(DeltaValueError):
        await plc.write_bits("D9002.1", True)
    assert mock.stats.requests == before

async def test_write_chunks_across_cap_and_block(plc, mock):
    before = mock.stats.writes
    await plc.write("D0", [0] * 101)
    assert mock.stats.writes - before == 2
    before = mock.stats.writes
    await plc.write("D4090", [0] * 10)
    assert mock.stats.writes - before == 2

async def test_write_timeout_closes_socket():
    from pymodbus.exceptions import ModbusIOException
    class FakeClient:
        connected = True
        calls = 0
        closes = 0
        async def connect(self): return True
        def close(self): FakeClient.closes += 1; FakeClient.connected = False
        async def write_register(self, *a, **k):
            FakeClient.calls += 1
            raise ModbusIOException("no response")
    plc = AsyncDeltaPLC("127.0.0.1")
    plc._client = FakeClient()
    with pytest.raises(DeltaTimeoutError):
        await plc.write("D0", 1)
    assert FakeClient.calls == 1
    assert FakeClient.closes == 1
    assert not plc.is_connected

async def test_write_retry_and_exception_not_retried():
    from pymodbus.pdu import ExceptionResponse
    class Ok:
        def isError(self): return False
    class Flaky:
        connected = True
        calls = 0
        async def connect(self): return True
        def close(self): pass
        async def write_register(self, *a, **k):
            Flaky.calls += 1
            if Flaky.calls == 1:
                from pymodbus.exceptions import ModbusIOException
                raise ModbusIOException("no response")
            return Ok()
    plc = AsyncDeltaPLC("127.0.0.1", write_retries=1)
    plc._client = Flaky()
    await plc.write("D0", 1)
    assert Flaky.calls == 2

    class Rejected:
        connected = True
        calls = 0
        async def connect(self): return True
        def close(self): pass
        async def write_register(self, *a, **k):
            Rejected.calls += 1
            return ExceptionResponse(6, exception_code=2)
    plc = AsyncDeltaPLC("127.0.0.1", write_retries=3)
    plc._client = Rejected()
    from delta_plc.errors import DeltaModbusException
    with pytest.raises(DeltaModbusException):
        await plc.write("D0", 1)
    assert Rejected.calls == 1
