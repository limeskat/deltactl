# tests/test_transport_detection.py
from __future__ import annotations

import pytest
from pymodbus.exceptions import ConnectionException

from deltactl import DeltaPLC
from deltactl.cli import main
from deltactl.errors import (
    AddressRangeError,
    ModelDetectionError,
    ModelMismatchError,
    UnknownModelCodeError,
)
from deltactl.mock import MockServer
from deltactl.planning import ReadGroup, make_tag, plan_reads
from deltactl.profiles import (
    DEFAULT_PROFILE,
    PROFILE_SE_EH3_SV2,
    PROFILE_SS2,
    Device,
    View,
)
from deltactl.transport import AsyncDeltaPLC


@pytest.fixture
def mock_se():
    server = MockServer(port=0, seed={"D1001": 0x7C21})
    server.start()
    yield server
    server.stop()


@pytest.fixture
def mock_ss2():
    server = MockServer(port=0, seed={"D1001": 0x7210})
    server.start()
    yield server
    server.stop()


@pytest.fixture
def mock_unknown():
    server = MockServer(port=0, seed={"D1001": 0xAA00})
    server.start()
    yield server
    server.stop()


async def test_auto_detection_resolves_profile(mock_se):
    async with AsyncDeltaPLC("127.0.0.1", port=mock_se.address[1], series="auto") as plc:
        assert plc.detected_profile == PROFILE_SE_EH3_SV2
        assert plc.effective_profile == PROFILE_SE_EH3_SV2


async def test_strict_mismatch_aborts_with_zero_payload_frames(mock_se):
    # Configured SS2 on SE hardware under strict_series=True
    plc = AsyncDeltaPLC("127.0.0.1", port=mock_se.address[1], series="SS2", strict_series=True)
    with pytest.raises(ModelMismatchError):
        await plc.connect()
    # Verified: exactly 1 probe request was received by mock, 0 writes
    assert mock_se.stats.requests == 1
    assert mock_se.stats.writes == 0
    assert plc.is_connected is False
    await plc.close()


async def test_socket_closed_and_not_connected_on_strict_detection_abort(mock_se):
    plc = AsyncDeltaPLC("127.0.0.1", port=mock_se.address[1], series="SS2", strict_series=True)
    with pytest.raises(ModelMismatchError):
        await plc.connect()
    assert plc.is_connected is False


async def test_unknown_model_code_strict(mock_unknown):
    plc = AsyncDeltaPLC(
        "127.0.0.1", port=mock_unknown.address[1], series="auto", strict_series=True
    )
    with pytest.raises(UnknownModelCodeError):
        await plc.connect()
    assert plc.is_connected is False


async def test_unknown_model_code_non_strict_warns_and_falls_back(mock_unknown):
    # Auto series: warns and falls back to DEFAULT_PROFILE
    plc = AsyncDeltaPLC(
        "127.0.0.1", port=mock_unknown.address[1], series="auto", strict_series=False
    )
    with pytest.warns(UserWarning, match=r"unrecognized model code 0xAA in D1001; using fallback"):
        await plc.connect()
    assert plc.detected_profile is None
    assert plc.effective_profile == DEFAULT_PROFILE
    assert plc.is_connected is True
    await plc.close()

    # Configured series: warns and uses configured profile
    plc2 = AsyncDeltaPLC(
        "127.0.0.1", port=mock_unknown.address[1], series="SS2", strict_series=False
    )
    with pytest.warns(
        UserWarning, match=r"unrecognized hardware model code 0xAA in D1001; using configured 'SS2'"
    ):
        await plc2.connect()
    assert plc2.detected_profile is None
    assert plc2.effective_profile == PROFILE_SS2
    assert plc2.is_connected is True
    await plc2.close()


async def test_probe_communication_failure_non_strict_warns_and_falls_back(mock_se, monkeypatch):
    async def fail_probe(self, *args, **kwargs):
        raise ConnectionException("simulated probe failure")

    plc = AsyncDeltaPLC("127.0.0.1", port=mock_se.address[1], series="auto", strict_series=False)
    monkeypatch.setattr(plc._client, "read_holding_registers", fail_probe.__get__(plc._client))
    with pytest.warns(
        UserWarning,
        match=r"failed to detect PLC model: .*simulated probe failure; using fallback profile",
    ):
        await plc.connect()
    assert plc.detected_profile is None
    assert plc.effective_profile == DEFAULT_PROFILE
    assert plc.is_connected is True
    await plc.close()

    # Also test with configured series
    plc_cfg = AsyncDeltaPLC("127.0.0.1", port=mock_se.address[1], series="SS2", strict_series=False)
    monkeypatch.setattr(
        plc_cfg._client, "read_holding_registers", fail_probe.__get__(plc_cfg._client)
    )
    with pytest.warns(
        UserWarning,
        match=r"could not verify hardware model: .*simulated probe failure; using configured 'SS2'",
    ):
        await plc_cfg.connect()
    assert plc_cfg.detected_profile is None
    assert plc_cfg.effective_profile == PROFILE_SS2
    assert plc_cfg.is_connected is True
    await plc_cfg.close()


async def test_probe_modbus_error_and_empty_registers_non_strict(mock_se, monkeypatch):
    class ModbusErrResp:
        def isError(self):
            return True

        def __str__(self):
            return "Exception 2"

    async def err_resp(self, *args, **kwargs):
        return ModbusErrResp()

    plc_err = AsyncDeltaPLC(
        "127.0.0.1", port=mock_se.address[1], series="auto", strict_series=False
    )
    monkeypatch.setattr(
        plc_err._client, "read_holding_registers", err_resp.__get__(plc_err._client)
    )
    with pytest.warns(UserWarning, match=r"Modbus error Exception 2; using fallback profile"):
        await plc_err.connect()
    assert plc_err.effective_profile == DEFAULT_PROFILE
    await plc_err.close()

    class EmptyResp:
        def __init__(self) -> None:
            self.registers: list[int] = []

        def isError(self) -> bool:
            return False

    async def empty_resp(self, *args, **kwargs):
        return EmptyResp()

    plc_empty = AsyncDeltaPLC(
        "127.0.0.1", port=mock_se.address[1], series="auto", strict_series=False
    )
    monkeypatch.setattr(
        plc_empty._client, "read_holding_registers", empty_resp.__get__(plc_empty._client)
    )
    with pytest.warns(UserWarning, match=r"empty response reading D1001; using fallback profile"):
        await plc_empty.connect()
    assert plc_empty.effective_profile == DEFAULT_PROFILE
    await plc_empty.close()


async def test_non_strict_mismatch_clamps_to_intersection(mock_ss2):
    # Configured SE on SS2 hardware: intersection clamps to SS2 bounds (D max 4999)
    async with AsyncDeltaPLC(
        "127.0.0.1", port=mock_ss2.address[1], series="SE", strict_series=False
    ) as plc:
        assert plc.detected_profile == PROFILE_SS2
        assert plc.effective_profile.devices[Device.D].views[View.WORD].segments[-1].last == 4999
        # Reading D4999 succeeds
        await plc.read("D4999")
        # Reading D5000 is blocked client-side by effective profile
        with pytest.raises(AddressRangeError):
            await plc.read("D5000")


async def test_non_strict_mismatch_warning_message(mock_se):
    # Configured SS2 on SE hardware (0x7C21) under strict_series=False
    plc = AsyncDeltaPLC("127.0.0.1", port=mock_se.address[1], series="SS2", strict_series=False)
    with pytest.warns(
        UserWarning,
        match=r"configured series 'SS2' differs from detected hardware 'SE_EH3_SV2' \(0x7C21\)",
    ):
        await plc.connect()
    assert plc.detected_profile == PROFILE_SE_EH3_SV2
    await plc.close()


async def test_reconnect_re_detection_aborts_on_hardware_model_change(mock_ss2, monkeypatch):
    # Avoid retry sleep delays
    async def no_sleep(_):
        pass

    monkeypatch.setattr("asyncio.sleep", no_sleep)

    plc = AsyncDeltaPLC(
        "127.0.0.1",
        port=mock_ss2.address[1],
        series="SS2",
        strict_series=True,
        read_retries=2,
        write_retries=2,
    )
    await plc.connect()
    assert plc.is_connected is True
    assert plc.detected_profile == PROFILE_SS2

    # Normal read succeeds
    assert await plc.read("D100") == 0

    # Modify mock server hardware model to SE (0x7C21)
    dev = mock_ss2._server.context.devices[0]
    start_address, _, registers, _ = dev.block["h"]
    registers[5097 - start_address] = 0x7C21

    # Simulate connection drop during read by having read_holding_registers drop connection on first call
    real_read = plc._client.read_holding_registers
    drop_done = False

    async def drop_once(address, count=1, **kwargs):
        nonlocal drop_done
        if not drop_done and address != 0x13E9:
            drop_done = True
            plc._client.close()
            raise ConnectionException("socket dropped during read")
        return await real_read(address, count=count, **kwargs)

    monkeypatch.setattr(plc._client, "read_holding_registers", drop_once)

    # In-flight read encounters dropped connection, triggers reconnect in retry loop,
    # reconnect re-probes D1001, detects SE != SS2, and aborts immediately with ModelMismatchError
    with pytest.raises(ModelMismatchError):
        await plc.read("D100")
    assert plc.is_connected is False

    await plc.close()

    # Also test write reconnection re-detection abort
    registers[5097 - start_address] = 0x7210  # restore to SS2
    await plc.connect()
    assert plc.is_connected is True

    registers[5097 - start_address] = 0x7C21  # change back to SE

    real_write = plc._client.write_register
    write_drop_done = False

    async def drop_write(address, value, **kwargs):
        nonlocal write_drop_done
        if not write_drop_done:
            write_drop_done = True
            plc._client.close()
            raise ConnectionException("socket dropped during write")
        return await real_write(address, value, **kwargs)

    monkeypatch.setattr(plc._client, "write_register", drop_write)

    writes_before = mock_ss2.stats.writes
    with pytest.raises(ModelMismatchError):
        await plc.write("D100", 99)
    assert plc.is_connected is False
    assert mock_ss2.stats.writes == writes_before

    await plc.close()


def test_delta_plc_facade_forwarding(mock_se):
    # With string series
    plc = DeltaPLC("127.0.0.1", port=mock_se.address[1], series="SE", strict_series=True)
    try:
        assert plc.series == "SE"
        assert plc.strict_series is True
        assert plc.configured_profile == PROFILE_SE_EH3_SV2
        assert plc.detected_profile is None
        assert plc.effective_profile == PROFILE_SE_EH3_SV2
        assert plc.is_connected is False
        plc.connect()
        assert plc.is_connected is True
        assert plc.detected_profile == PROFILE_SE_EH3_SV2
        assert plc.effective_profile == PROFILE_SE_EH3_SV2
    finally:
        plc.close()

    # With Profile object
    plc2 = DeltaPLC("127.0.0.1", port=mock_se.address[1], series=PROFILE_SS2, strict_series=False)
    try:
        assert plc2.series == PROFILE_SS2
        assert plc2.strict_series is False
        assert plc2.configured_profile == PROFILE_SS2
    finally:
        plc2.close()


async def test_auto_connect_rejects_out_of_range_for_detected_profile(mock_ss2):
    # SS2 mock has D max 4999.
    # auto_connect=True and series="auto": client initially defaults to DEFAULT_PROFILE (D max 9999).
    # On first operation, auto-connect probes D1001 and resolves SS2 profile.
    # Reads, writes, and read_many to D5000 must be rejected with AddressRangeError.
    plc = AsyncDeltaPLC("127.0.0.1", port=mock_ss2.address[1], series="auto", auto_connect=True)
    assert not plc.is_connected

    # read("D5000") rejected with AddressRangeError
    with pytest.raises(AddressRangeError):
        await plc.read("D5000")
    assert plc.is_connected
    assert plc.effective_profile == PROFILE_SS2

    # read_many(["D5000"]) rejected with AddressRangeError
    with pytest.raises(AddressRangeError):
        await plc.read_many(["D5000"])

    # write("D5000", 1) rejected with AddressRangeError
    writes_before = mock_ss2.stats.writes
    with pytest.raises(AddressRangeError):
        await plc.write("D5000", 1)
    assert mock_ss2.stats.writes == writes_before

    # D4999 succeeds
    assert await plc.read("D4999") == 0

    await plc.close()


async def test_auto_connect_first_op_read_many_rejects_d5000(mock_ss2):
    plc = AsyncDeltaPLC("127.0.0.1", port=mock_ss2.address[1], series="auto", auto_connect=True)
    with pytest.raises(AddressRangeError):
        await plc.read_many(["D5000"])
    assert plc.is_connected
    assert plc.effective_profile == PROFILE_SS2
    await plc.close()


async def test_auto_connect_first_op_write_rejects_d5000(mock_ss2):
    plc = AsyncDeltaPLC("127.0.0.1", port=mock_ss2.address[1], series="auto", auto_connect=True)
    writes_before = mock_ss2.stats.writes
    with pytest.raises(AddressRangeError):
        await plc.write("D5000", 10)
    assert mock_ss2.stats.writes == writes_before
    assert plc.is_connected
    assert plc.effective_profile == PROFILE_SS2
    await plc.close()


async def test_strict_detection_failure_aborts():
    # Dead port under strict_series=True
    plc = AsyncDeltaPLC("127.0.0.1", port=1, timeout=0.1, series="auto", strict_series=True)
    with pytest.raises(ModelDetectionError):
        await plc.connect()
    assert plc.is_connected is False


async def test_read_planned_out_of_range_raises_and_sends_zero_frames(mock_ss2):
    async with AsyncDeltaPLC("127.0.0.1", port=mock_ss2.address[1], series="SS2") as plc:
        initial_requests = mock_ss2.stats.requests
        group = plan_reads([make_tag("D8000")])[0]
        with pytest.raises(AddressRangeError):
            await plc.read_planned([group])
        # Assert zero wire frames sent to mock
        assert mock_ss2.stats.requests == initial_requests

    # Also unconnected client raises AddressRangeError without sending any frames
    plc_unconnected = AsyncDeltaPLC("127.0.0.1", port=mock_ss2.address[1], series="SS2")
    with pytest.raises(AddressRangeError):
        await plc_unconnected.read_planned([group])
    assert mock_ss2.stats.requests == initial_requests
    assert plc_unconnected.is_connected is False


async def test_read_planned_tagless_and_slot_validation(mock_ss2):
    async with AsyncDeltaPLC("127.0.0.1", port=mock_ss2.address[1], series="SS2") as plc:
        # tagless ReadGroup(3, 0x0F00, 2, ()) -> AddressRangeError, zero new mock requests
        reqs = mock_ss2.stats.requests
        with pytest.raises(AddressRangeError):
            await plc.read_planned([ReadGroup(3, 0x0F00, 2, ())])
        assert mock_ss2.stats.requests == reqs

        # tagless ReadGroup(3, 0x0EC8, 4, ()) -> AddressRangeError, zero new requests
        with pytest.raises(AddressRangeError):
            await plc.read_planned([ReadGroup(3, 0x0EC8, 4, ())])
        assert mock_ss2.stats.requests == reqs

        # tagged ReadGroup(3, 0x0EC8, 4, (make_tag("C200.v"), make_tag("C201.v"))) -> AddressRangeError, zero new requests
        c200_tag = make_tag("C200.v")
        c201_tag = make_tag("C201.v")
        with pytest.raises(AddressRangeError):
            await plc.read_planned([ReadGroup(3, 0x0EC8, 4, (c200_tag, c201_tag))])
        assert mock_ss2.stats.requests == reqs

        # tagged group with tag outside span -> AddressRangeError, zero new requests
        with pytest.raises(AddressRangeError, match="lies outside its group span"):
            await plc.read_planned([ReadGroup(3, 0x1000, 1, (make_tag("D105"),))])
        assert mock_ss2.stats.requests == reqs

        # valid tagless ReadGroup(3, 0x0EC8, 2, ()) -> one request and a result
        mock_ss2.set_values("C200.v", 42)
        res = await plc.read_planned([ReadGroup(3, 0x0EC8, 2, ())])
        assert mock_ss2.stats.requests == reqs + 1
        assert len(res) == 1
        assert res[0].error is None
        assert len(res[0].registers) == 2

        # optional: tagless ReadGroup(1, 0x0000, 1, ()) is valid (S0)
        res_s0 = await plc.read_planned([ReadGroup(1, 0x0000, 1, ())])
        assert mock_ss2.stats.requests == reqs + 2
        assert len(res_s0) == 1
        assert res_s0[0].error is None
        assert len(res_s0[0].bits) == 1


def test_watch_out_of_range_series_fails_exit_2(mock_ss2):
    assert (
        main(
            [
                "watch",
                "D8000",
                "--series",
                "SS2",
                "--host",
                "127.0.0.1",
                "--port",
                str(mock_ss2.address[1]),
                "--count",
                "1",
            ]
        )
        == 2
    )
