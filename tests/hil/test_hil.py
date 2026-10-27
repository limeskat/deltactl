# tests/hil/test_hil.py
import os

import pytest

from deltactl import AsyncDeltaPLC

HOST = os.environ.get("DELTA_PLC_HIL_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="set DELTA_PLC_HIL_HOST to run HIL tests")


@pytest.fixture
async def plc():
    client = AsyncDeltaPLC(HOST, unit=int(os.environ.get("DELTA_PLC_HIL_UNIT", "1")), timeout=2)
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
