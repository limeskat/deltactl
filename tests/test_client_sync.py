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
        name for name, obj in inspect.getmembers(AsyncDeltaPLC, inspect.isfunction)
        if not name.startswith("_") and name not in {"is_connected"}
    }
    sync_methods = {
        name for name, obj in inspect.getmembers(DeltaPLC, inspect.isfunction)
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
