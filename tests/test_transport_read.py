import asyncio
import pytest
from delta_plc.mock import MockServer
from delta_plc.transport import AsyncDeltaPLC
from delta_plc.errors import (
    BlockBoundaryError, DeltaModbusException, DeltaProtocolError,
    DeltaValueError, NotConnectedError,
)

@pytest.fixture(scope="module")
def mock():
    server = MockServer(port=0, seed={"D200": 0x5678, "D201": 0x1234, "D300": 0xFFFE})
    server.start()
    yield server
    server.stop()

@pytest.fixture
async def plc(mock):
    client = AsyncDeltaPLC("127.0.0.1", port=mock.address[1], timeout=1)
    await client.connect()
    yield client
    await client.close()

async def test_explicit_connect_required(mock):
    client = AsyncDeltaPLC("127.0.0.1", port=mock.address[1])
    with pytest.raises(NotConnectedError):
        await client.read("D100")
    await client.connect()
    assert client.is_connected and await client.read("D1000") == 200
    await client.close()
    assert not client.is_connected

async def test_reads_all_devices(plc):
    assert await plc.read("D1000") == 200
    assert await plc.read("M1000") is True
    assert await plc.read("X0") is False
    assert await plc.read("Y0") is False
    assert await plc.read("D1000.3") is True
    assert await plc.read_bits("M0", count=2) == [False, False]

async def test_one_socket_per_client():
    server = MockServer(port=0)
    server.start()
    try:
        client = AsyncDeltaPLC("127.0.0.1", port=server.address[1], timeout=1)
        await client.connect()
        try:
            for _ in range(5):
                assert await client.read("D0") == 0
            assert server.stats.clients == 1
        finally:
            await client.close()
    finally:
        server.stop()

async def test_32bit_cdab_and_signed(plc):
    assert await plc.read_i32("D200") == 0x12345678
    assert await plc.read_u32("D200") == 0x12345678
    assert await plc.read_i16("D300") == -2
    assert await plc.read("D300") == 0xFFFE

async def test_count_chunking_and_split_block(plc, mock):
    before = mock.stats.requests
    assert len(await plc.read("D0", count=101)) == 101
    assert mock.stats.requests - before == 2
    before = mock.stats.requests
    assert len(await plc.read("D4000", count=200)) == 200
    assert mock.stats.requests - before == 3

async def test_read_many_order_and_dedup_error(plc):
    values = await plc.read_many(["D1000", "M1000", "D200:i32"])
    assert list(values) == ["D1000", "M1000", "D200:i32"]
    with pytest.raises(DeltaValueError):
        await plc.read_many(["D100", "d100"])

async def test_bad_count_and_boundary(plc):
    for bad in (0, -1):
        with pytest.raises(DeltaValueError):
            await plc.read("D0", count=bad)
    with pytest.raises(BlockBoundaryError):
        await plc.read_f32("D4095")
    with pytest.raises(DeltaValueError):
        await plc.read_bits("D100.3")

async def test_short_response_is_protocol_error():
    class FakeResponse:
        def isError(self): return False
        registers = [1]
    class FakeClient:
        connected = True
        calls = 0
        async def connect(self): return True
        def close(self): pass
        async def read_holding_registers(self, *a, **k):
            FakeClient.calls += 1
            return FakeResponse()
    plc = AsyncDeltaPLC("127.0.0.1")
    plc._client = FakeClient()
    with pytest.raises(DeltaProtocolError):
        await plc.read("D0", count=2)
    assert FakeClient.calls == 1

async def test_exception_response_not_retried():
    from pymodbus.pdu import ExceptionResponse
    class FakeClient:
        connected = True
        calls = 0
        async def connect(self): return True
        def close(self): pass
        async def read_holding_registers(self, *a, **k):
            FakeClient.calls += 1
            return ExceptionResponse(3, exception_code=2)
    plc = AsyncDeltaPLC("127.0.0.1")
    plc._client = FakeClient()
    with pytest.raises(DeltaModbusException) as exc:
        await plc.read("D0")
    assert (exc.value.function_code, exc.value.exception_code) == (3, 2)
    assert FakeClient.calls == 1

async def test_read_retries_then_succeeds(monkeypatch):
    from pymodbus.exceptions import ModbusIOException
    class Ok:
        def isError(self): return False
        registers = [7]
    class Flaky:
        connected = True
        calls = 0
        async def connect(self): return True
        def close(self): pass
        async def read_holding_registers(self, *a, **k):
            Flaky.calls += 1
            if Flaky.calls < 3:
                raise ModbusIOException("no response")
            return Ok()
    async def no_sleep(_): pass
    monkeypatch.setattr("asyncio.sleep", no_sleep)
    plc = AsyncDeltaPLC("127.0.0.1", read_retries=2)
    plc._client = Flaky()
    assert await plc.read("D0") == 7
    assert Flaky.calls == 3

async def test_read_planned_short_circuits_on_connection_error():
    from pymodbus.exceptions import ConnectionException
    from delta_plc.planning import make_tags, plan_reads
    class Down:
        connected = True
        coil_calls = 0
        async def connect(self): return True
        def close(self): pass
        async def read_holding_registers(self, *a, **k):
            raise ConnectionException("reset")
        async def read_coils(self, *a, **k):
            Down.coil_calls += 1
            raise AssertionError("must not be called")
    groups = plan_reads(make_tags(["D0", "M0"]))
    plc = AsyncDeltaPLC("127.0.0.1")
    plc._client = Down()
    results = await plc.read_planned(groups)
    assert results[0].error is not None and results[1].error is not None
    assert Down.coil_calls == 0
