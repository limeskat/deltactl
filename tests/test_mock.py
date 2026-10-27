import asyncio

import pytest
from pymodbus.client import AsyncModbusTcpClient

from deltactl.mock import MockServer


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
