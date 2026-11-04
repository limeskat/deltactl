# tests/hil/test_hil.py
import os

import pytest
from pymodbus.client import AsyncModbusTcpClient

from deltactl import AddressRangeError, AsyncDeltaPLC
from deltactl.packing import DType, decode_value

HOST = os.environ.get("DELTA_PLC_HIL_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="set DELTA_PLC_HIL_HOST to run HIL tests")


def _parse_host(host_str: str | None) -> tuple[str, int]:
    if not host_str:
        return "", 502
    port_env = os.environ.get("DELTA_PLC_HIL_PORT")
    default_port = int(port_env) if port_env else 502
    if ":" in host_str:
        parts = host_str.rsplit(":", 1)
        h = parts[0].strip("[]")
        try:
            return h, int(parts[1])
        except ValueError:
            return host_str, default_port
    return host_str, default_port


@pytest.fixture
async def plc():
    host, port = _parse_host(HOST)
    client = AsyncDeltaPLC(
        host,
        port=port,
        unit=int(os.environ.get("DELTA_PLC_HIL_UNIT", "1")),
        timeout=2,
    )
    await client.connect()
    yield client
    await client.close()


async def test_specials(plc):
    assert await plc.read("D1000") == 200
    assert await plc.read("D1001") == 0x7C21
    assert await plc.read("M1000") is True
    assert await plc.read("M1067") is False
    assert await plc.read("D1067") == 0


@pytest.mark.skipif(os.environ.get("DELTA_PLC_HIL_WRITES") != "1", reason="writes disabled")
async def test_scratch_writes(plc):
    await plc.write("D9000", 1234)
    assert await plc.read("D9000") == 1234
    await plc.write("D9000", 0)
    await plc.write_bits("M900", [True, False])
    assert await plc.read_bits("M900", count=2) == [True, False]
    await plc.write_bits("M900", [False, False])


async def test_stc_types(plc):
    t0_c = await plc.read("T0.c")
    t0_v = await plc.read("T0.v")
    c0_c = await plc.read("C0.c")
    c0_v = await plc.read("C0.v")
    s0 = await plc.read("S0")

    assert type(t0_c) is bool
    assert type(t0_v) is int
    assert type(c0_c) is bool
    assert type(c0_v) is int
    assert type(s0) is bool


async def test_c32_counters(plc):
    try:
        c200 = await plc.read("C200.v")
        c201 = await plc.read("C201.v")
    except AddressRangeError as err:
        pytest.skip(f"series has no 32-bit counters: {err}")

    assert type(c200) is int
    assert type(c201) is int


async def test_c32_slot_proof(plc):
    try:
        c200 = await plc.read("C200.v")
        c201 = await plc.read("C201.v")
    except AddressRangeError as err:
        pytest.skip(f"series has no 32-bit counters: {err}")

    await plc.close()
    client = AsyncModbusTcpClient(plc.host, port=plc.port, timeout=plc.timeout)
    await client.connect()
    try:
        resp = await client.read_holding_registers(0x0EC9, count=2, device_id=plc.unit)
        assert not resp.isError()
        registers = resp.registers
        assert len(registers) == 2
        decoded = decode_value(registers, DType.I32)
        assert decoded == c201
        if c200 != c201:
            assert decoded != c200
    finally:
        client.close()
