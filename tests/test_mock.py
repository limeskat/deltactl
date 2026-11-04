import asyncio
import inspect
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.server.requesthandler import ServerRequestHandler

from deltactl import DeltaPLC
from deltactl.errors import ReadOnlyAddressError
from deltactl.mock import MockServer, MockStats, _MockHandler


@pytest.fixture
def mock():
    server = MockServer(port=0, seed={"D100": 42, "M0": True, "X0": True, "Y0": True})
    server.start()
    yield server
    server.stop()


async def _client(port):
    c = AsyncModbusTcpClient("127.0.0.1", port=port, timeout=1, retries=0)
    assert await c.connect()
    return c


async def test_defaults_and_wildcard_unit(mock):
    c = await _client(mock.address[1])
    try:
        rr = await c.read_holding_registers(0x1000 + 1000, count=1, device_id=7)
        assert rr.registers == [200]
    finally:
        c.close()


async def test_seed_and_x_fc02(mock):
    c = await _client(mock.address[1])
    try:
        assert (await c.read_holding_registers(0x1000 + 100, count=1, device_id=1)).registers == [
            42
        ]
        assert (await c.read_discrete_inputs(0x0400, count=1, device_id=1)).bits[0] is True
        assert (await c.read_coils(0x0400, count=1, device_id=1)).isError()
        assert (await c.read_coils(0x0500, count=1, device_id=1)).bits[0] is True
    finally:
        c.close()


async def test_guards_and_readonly(mock):
    c = await _client(mock.address[1])
    try:
        for addr, values in [(0x1000 + 1000, [1]), (0x1000 + 999, [1, 2])]:
            rr = await c.write_registers(addr, values, device_id=1)
            assert rr.isError() and rr.exception_code == 2
        for addr in (0x0800 + 1000, 0x0800 + 1067):
            rr = await c.write_coils(addr, [True], device_id=1)
            assert rr.isError() and rr.exception_code == 2
        assert (await c.write_coil(0x0400, True, device_id=1)).exception_code == 2
        assert not (await c.write_coil(0x0500, True, device_id=1)).isError()
    finally:
        c.close()


async def test_pdu_caps(mock):
    c = await _client(mock.address[1])
    try:
        assert (await c.read_holding_registers(0x1000, count=101, device_id=1)).exception_code == 3
        assert (await c.read_coils(0x0800, count=257, device_id=1)).exception_code == 3
        assert (await c.write_registers(0x2000, [0] * 101, device_id=1)).exception_code == 3
        assert (await c.write_coils(0x0800, [False] * 257, device_id=1)).exception_code == 3
    finally:
        c.close()


def test_fault_and_stop_flags():
    server = MockServer(port=0, fault_code=5, plc_stop=True, fixed_scan_ms=2.5)
    server.start()
    try:

        async def scenario():
            c = AsyncModbusTcpClient("127.0.0.1", port=server.address[1], timeout=1, retries=0)
            await c.connect()
            try:
                assert (
                    await c.read_holding_registers(0x1000 + 1067, count=1, device_id=1)
                ).registers == [5]
                assert (await c.read_coils(0x0800 + 1067, count=1, device_id=1)).bits[0] is True
                assert (await c.read_coils(0x0800 + 1000, count=1, device_id=1)).bits[0] is False
                regs = await c.read_holding_registers(0x1000 + 1010, count=3, device_id=1)
                assert regs.registers == [25, 25, 25]
            finally:
                c.close()

        asyncio.run(scenario())
    finally:
        server.stop()


def test_max_clients():
    server = MockServer(port=0, max_clients=4)
    server.start()

    async def scenario():
        clients = []
        try:
            for _ in range(5):
                c = AsyncModbusTcpClient(
                    "127.0.0.1", port=server.address[1], timeout=0.5, retries=0
                )
                try:
                    await c.connect()
                except OSError:
                    pass
                clients.append(c)
            await asyncio.sleep(0.2)
            assert server.stats.clients == 4
        finally:
            for c in clients:
                c.close()

    try:
        asyncio.run(scenario())
    finally:
        server.stop()


def test_live_scan_moves_scan_registers():
    server = MockServer(port=0, live_scan=True)
    server.start()
    try:

        async def scenario():
            c = AsyncModbusTcpClient("127.0.0.1", port=server.address[1], timeout=1, retries=0)
            await c.connect()
            try:
                rr = await c.read_holding_registers(0x1000 + 1010, count=3, device_id=1)
                assert len(rr.registers) == 3
                assert all(v >= 1 for v in rr.registers)
            finally:
                c.close()

        asyncio.run(scenario())
    finally:
        server.stop()


def test_seed_validation_errors():
    with pytest.raises(ValueError, match="bare tag"):
        MockServer(port=0, seed={"D100.1": 1})
    with pytest.raises(ValueError, match="bare tag"):
        MockServer(port=0, seed={"D100:f32": 1})
    with pytest.raises(ValueError, match="integers in 0..65535"):
        MockServer(port=0, seed={"D100": 70000})
    with pytest.raises(ValueError, match="integers in 0..65535"):
        MockServer(port=0, seed={"D100": "abc"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="booleans"):
        MockServer(port=0, seed={"M0": 123})  # type: ignore[dict-item]


def test_server_lifecycle_idempotency():
    server = MockServer(port=0)
    server.start()
    try:
        server.start()
        assert server.running
    finally:
        server.stop()
        server.stop()
        assert not server.running


def test_station_id_validation():
    with pytest.raises(ValueError, match="0..65535"):
        MockServer(port=0, station_id=-1)
    with pytest.raises(ValueError, match="0..65535"):
        MockServer(port=0, station_id=65536)


def test_set_values_runtime_update():
    server = MockServer(port=0)
    server.start()
    try:
        server.set_values("D500", 39)
        server.set_values("M100", True)

        async def scenario():
            c = AsyncModbusTcpClient("127.0.0.1", port=server.address[1], timeout=1, retries=0)
            await c.connect()
            try:
                rr = await c.read_holding_registers(0x1000 + 500, count=1, device_id=1)
                assert rr.registers == [39]
                rc = await c.read_coils(0x0800 + 100, count=1, device_id=1)
                assert rc.bits[0] is True
            finally:
                c.close()

        asyncio.run(scenario())
    finally:
        server.stop()


@contextmanager
def _running(**kwargs):
    server = MockServer(port=0, **kwargs)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_timer_and_counter_round_trip():
    with (
        _running(
            seed={
                "T0.v": 42,
                "T0.c": True,
                "C0.v": 7,
                "C0.c": False,
                "S3": True,
            }
        ) as mock,
        DeltaPLC(mock.address[0], port=mock.address[1]) as plc,
    ):
        assert plc.read("T0.v") == 42
        assert plc.read("T0.c") is True
        assert plc.read("C0.v") == 7
        assert plc.read("C0.c") is False
        assert plc.read("S3") is True


def test_c32_slot_reads_are_address_based():
    with (
        _running(seed={"C200.v": 0x11223344, "C201.v": -1}) as mock,
        DeltaPLC(mock.address[0], port=mock.address[1]) as plc,
    ):
        assert plc.read("C200.v") == 0x11223344
        assert plc.read("C201.v") == -1


async def test_c32_block_read_is_rejected():
    with _running() as mock:
        c = await _client(mock.address[1])
        try:
            res = await c.read_holding_registers(0x0EC8, count=4, device_id=1)
            assert res.isError() and res.exception_code == 3
        finally:
            c.close()


@pytest.mark.parametrize("tag,value", [("T0.v", 1), ("S0", True), ("C0.c", True)])
def test_driver_refuses_tc_writes_without_a_pdu(tag, value):
    with _running() as mock, DeltaPLC(mock.address[0], port=mock.address[1]) as plc:
        with pytest.raises(ReadOnlyAddressError):
            plc.write(tag, value)
        assert mock.stats.writes == 0


def test_set_values_stc():
    with _running() as mock:
        mock.set_values("T0.v", 99)
        mock.set_values("T0.c", True)
        mock.set_values("C0.v", 123)
        mock.set_values("C0.c", True)
        mock.set_values("S0", True)
        mock.set_values("C200.v", -500)
        mock.set_values("C254.v", 0x12345678)
        with DeltaPLC(mock.address[0], port=mock.address[1]) as plc:
            assert plc.read("T0.v") == 99
            assert plc.read("T0.c") is True
            assert plc.read("C0.v") == 123
            assert plc.read("C0.c") is True
            assert plc.read("S0") is True
            assert plc.read("C200.v") == -500
            assert plc.read("C254.v") == 0x12345678


async def test_modbus_writes_rejected_for_stc_and_c32():
    with _running() as mock:
        c = await _client(mock.address[1])
        try:
            # FC06 / FC16 to T, C16, C32 must fail with ILLEGAL_ADDRESS (2)
            for addr in (0x0600, 0x0E00, 0x0EC8):
                res6 = await c.write_register(addr, 1, device_id=1)
                assert res6.isError() and res6.exception_code == 2
                res16 = await c.write_registers(addr, [1, 2], device_id=1)
                assert res16.isError() and res16.exception_code == 2
            # FC05 / FC15 to S, T contact, C contact must fail with ILLEGAL_ADDRESS (2)
            for addr in (0x0000, 0x0600, 0x0E00, 0x0EC8):
                res5 = await c.write_coil(addr, True, device_id=1)
                assert res5.isError() and res5.exception_code == 2
                res15 = await c.write_coils(addr, [True], device_id=1)
                assert res15.isError() and res15.exception_code == 2
        finally:
            c.close()


async def test_c32_invalid_counts_and_straddles_rejected():
    with _running() as mock:
        c = await _client(mock.address[1])
        try:
            # Count 1 on C32 -> ILLEGAL_VALUE (3)
            res = await c.read_holding_registers(0x0EC8, count=1, device_id=1)
            assert res.isError() and res.exception_code == 3

            # Count 3 on C32 -> ILLEGAL_VALUE (3)
            res = await c.read_holding_registers(0x0EC8, count=3, device_id=1)
            assert res.isError() and res.exception_code == 3

            # Straddling C16 and C32 -> ILLEGAL_VALUE (3)
            res = await c.read_holding_registers(0x0EC7, count=2, device_id=1)
            assert res.isError() and res.exception_code == 3
        finally:
            c.close()


def test_seed_and_set_values_validation_stc():
    with pytest.raises(ValueError, match="0..65535"):
        MockServer(port=0, seed={"T0.v": 70000})
    with pytest.raises(ValueError, match="0..65535"):
        MockServer(port=0, seed={"C0.v": -1})
    with pytest.raises(ValueError, match="booleans"):
        MockServer(port=0, seed={"S0": 1})
    with pytest.raises(ValueError, match="booleans"):
        MockServer(port=0, seed={"T0.c": 1})
    with pytest.raises(ValueError, match="booleans"):
        MockServer(port=0, seed={"C0.c": 1})
    with pytest.raises(ValueError, match="-2147483648..4294967295"):
        MockServer(port=0, seed={"C200.v": 2**32})
    with pytest.raises(ValueError, match="-2147483648..4294967295"):
        MockServer(port=0, seed={"C200.v": -(2**31) - 1})
    with pytest.raises(ValueError, match="-2147483648..4294967295"):
        MockServer(port=0, seed={"C200.v": True})

    with _running() as mock:
        with pytest.raises(ValueError, match="-2147483648..4294967295"):
            mock.set_values("C200.v", 2**32)
        with pytest.raises(ValueError, match="-2147483648..4294967295"):
            mock.set_values("C200.v", True)
        with pytest.raises(ValueError, match="0..65535"):
            mock.set_values("T0.v", 70000)
        with pytest.raises(ValueError, match="booleans"):
            mock.set_values("S0", 123)


async def test_mock_handler_handle_request_accepts_positional_pdu():
    sig = inspect.signature(_MockHandler.handle_request)
    assert any(
        p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for name, p in sig.parameters.items()
        if name != "self"
    )

    handler = _MockHandler.__new__(_MockHandler)
    handler.stats = MockStats()
    handler.values = {}
    handler.last_pdu = None

    pdu = MagicMock()
    pdu.function_code = 3
    pdu.address = 0x1000
    pdu.count = 1

    # pymodbus 3.16.2 signature: handle_request(pdu)
    with patch.object(ServerRequestHandler, "handle_request", new_callable=AsyncMock) as mock_super:
        await handler.handle_request(pdu)
        mock_super.assert_awaited_once_with(pdu)
        assert handler.stats.requests == 1

    # pymodbus 3.16.1 signature: handle_request() with last_pdu fallback
    handler.last_pdu = pdu
    with patch.object(ServerRequestHandler, "handle_request", new_callable=AsyncMock) as mock_super:
        await handler.handle_request()
        mock_super.assert_awaited_once_with()
        assert handler.stats.requests == 2

    # rejection path with positional pdu
    bad_pdu = MagicMock()
    bad_pdu.function_code = 3
    bad_pdu.address = 0x0000
    bad_pdu.count = 1
    bad_pdu.transaction_id = 1
    bad_pdu.dev_id = 1
    handler.server_send = MagicMock()
    handler.last_addr = ("127.0.0.1", 502)
    await handler.handle_request(bad_pdu)
    assert handler.stats.rejected == 1
    assert handler.server_send.called
