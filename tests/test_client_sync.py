# tests/test_client_sync.py
import asyncio
import concurrent.futures
import inspect
import threading
import time

import pytest

from deltactl import AsyncDeltaPLC, DeltaPLC
from deltactl.errors import DeltaTimeoutError
from deltactl.mock import MockServer


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
    with pytest.raises(RuntimeError, match="DeltaPLC is closed"):
        plc.read("D100")


def test_atexit_registered_and_unregistered(monkeypatch):
    callbacks = []
    monkeypatch.setattr("deltactl.client.atexit.register", callbacks.append)
    monkeypatch.setattr("deltactl.client.atexit.unregister", callbacks.remove)
    plc = DeltaPLC("127.0.0.1")
    assert plc.close in callbacks
    plc.close()
    assert plc.close not in callbacks


def test_sync_call_timeout_raises_and_cancels(monkeypatch, mock):
    plc = DeltaPLC("127.0.0.1", port=mock.address[1], sync_call_timeout=0.05)
    try:

        async def slow_read(*args, **kwargs):
            await asyncio.sleep(1.0)
            return 42

        monkeypatch.setattr(plc._client, "read", slow_read)

        submitted_futures = []
        original_submit = plc._loop_thread.submit

        def spy_submit(coro):
            fut = original_submit(coro)
            submitted_futures.append(fut)
            return fut

        monkeypatch.setattr(plc._loop_thread, "submit", spy_submit)

        with pytest.raises(DeltaTimeoutError, match="sync call timed out after 0.05s"):
            plc.read("D100")

        assert len(submitted_futures) == 1
        assert submitted_futures[0].cancelled()
    finally:
        plc.close()


def test_submission_lock_serializes_threads(mock):
    plc = DeltaPLC("127.0.0.1", port=mock.address[1])
    try:
        plc.connect()
        assert isinstance(plc._submission_lock, type(threading.Lock()))

        active_calls = 0
        max_active = 0
        call_lock = threading.Lock()
        original_submit = plc._loop_thread.submit

        def tracked_submit(coro):
            nonlocal active_calls, max_active
            with call_lock:
                active_calls += 1
                max_active = max(max_active, active_calls)
            try:
                time.sleep(0.02)
                return original_submit(coro)
            finally:
                with call_lock:
                    active_calls -= 1

        plc._loop_thread.submit = tracked_submit

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(plc.read, "D100") for _ in range(10)]
            results = [f.result() for f in futures]

        assert results == [42] * 10
        assert max_active == 1
    finally:
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
    from deltactl.planning import make_tags, plan_reads

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
