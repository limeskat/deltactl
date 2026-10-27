# tests/test_client_sync.py
import inspect

import pytest

from delta_plc import AsyncDeltaPLC, DeltaPLC
from delta_plc.mock import MockServer


@pytest.fixture(scope="module")
def mock():
    server = MockServer(port=0, seed={"D100": 42})
    server.start()
    yield server
    server.stop()


def test_signature_parity():
    async_methods = {
        name
        for name, obj in inspect.getmembers(AsyncDeltaPLC, inspect.isfunction)
        if not name.startswith("_") and name not in {"is_connected"}
    }
    sync_methods = {
        name
        for name, obj in inspect.getmembers(DeltaPLC, inspect.isfunction)
        if not name.startswith("_")
    }
    assert async_methods <= sync_methods


def test_read_write_and_context_manager(mock):
    with DeltaPLC("127.0.0.1", port=mock.address[1]) as plc:
        assert plc.read("D100") == 42
        plc.write("D9000", 7)
        assert plc.read("D9000") == 7
        assert plc.read_many(["D100", "M1000"]) == {"D100": 42, "M1000": True}
    assert not plc.is_connected


def test_close_idempotent(mock):
    plc = DeltaPLC("127.0.0.1", port=mock.address[1])
    plc.connect()
    plc.close()
    plc.close()


def test_atexit_registered(monkeypatch):
    registered = []
    monkeypatch.setattr("delta_plc.client.atexit.register", lambda fn: registered.append(fn))
    plc = DeltaPLC("127.0.0.1")
    assert registered
    plc.close()


def test_self_call_from_loop_thread_raises(mock):
    plc = DeltaPLC("127.0.0.1", port=mock.address[1])
    try:
        plc.connect()

        def call_from_loop():
            with pytest.raises(RuntimeError):
                plc.read("D100")

        future = plc._submit_raw(call_from_loop)
        future.result(5)
    finally:
        plc.close()


def test_all_sync_read_write_variants(mock):
    from delta_plc.planning import make_tags, plan_reads

    with DeltaPLC("127.0.0.1", port=mock.address[1]) as plc:
        plc.write_u16("D9001", 1234)
        assert plc.read_u16("D9001") == 1234
        plc.write_i16("D9002", -5)
        assert plc.read_i16("D9002") == -5

        plc.write_i32("D9004", -123456)
        assert plc.read_i32("D9004") == -123456
        plc.write_u32("D9006", 0xDEADBEEF)
        assert plc.read_u32("D9006") == 0xDEADBEEF

        plc.write_f32("D9008", 12.5)
        assert plc.read_f32("D9008") == pytest.approx(12.5)

        plc.write_bits("M900", [True, False])
        assert plc.read_bits("M900", count=2) == [True, False]

        groups = plan_reads(make_tags(["D9001", "D9002"]))
        results = plc.read_planned(groups)
        assert len(results) == 1
        assert results[0].error is None
