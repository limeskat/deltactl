"""Synchronous facade over the async Delta DVP transport.

:class:`DeltaPLC` owns one private event loop on one daemon thread. Every
public method mirrors :class:`~deltactl.transport.AsyncDeltaPLC`, submits the
matching coroutine with ``asyncio.run_coroutine_threadsafe`` and blocks for the
result. Scripts and the CLI use this surface; asyncio programs should use
:class:`AsyncDeltaPLC` directly.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextlib
import threading
from collections.abc import Callable, Coroutine, Sequence
from concurrent.futures import Future
from typing import Any, TypeVar

from .errors import DeltaTimeoutError
from .planning import ReadGroup
from .profiles import SeriesProfile
from .transport import AsyncDeltaPLC, GroupResult

__all__ = ["DeltaPLC"]

_T = TypeVar("_T")

_THREAD_ERROR = "sync DeltaPLC method called from its own event loop thread"


class _LoopThread:
    """One event loop on one daemon thread, started on first use."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    @property
    def thread_id(self) -> int | None:
        """The loop thread's ident, or ``None`` before the loop starts."""
        thread = self._thread
        return None if thread is None else thread.ident

    def submit(self, coro: Coroutine[Any, Any, _T]) -> Future[_T]:
        """Schedule *coro* on the loop thread without waiting for it."""
        loop = self._loop
        if loop is None:
            with self._start_lock:
                loop = self._loop
                if loop is None:
                    loop = asyncio.new_event_loop()
                    thread = threading.Thread(
                        target=self._run_loop,
                        args=(loop,),
                        name="delta-plc-sync",
                        daemon=True,
                    )
                    self._loop = loop
                    self._thread = thread
                    thread.start()
        return asyncio.run_coroutine_threadsafe(coro, loop)

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        """Thread body: run the loop until stopped, then close it."""
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            loop.close()

    def stop(self, timeout: float) -> None:
        """Stop and join the loop thread; a no-op before first use."""
        loop, thread = self._loop, self._thread
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout)


class DeltaPLC:
    """Blocking Delta DVP client wrapping :class:`AsyncDeltaPLC`.

    Constructor parameters are identical to the async client plus
    ``sync_call_timeout``, the longest any single call waits for the loop
    thread (default 120 s).
    """

    def __init__(
        self,
        host: str,
        *,
        port: int = 502,
        unit: int = 1,
        timeout: float = 2.0,
        connect_timeout: float = 3.0,
        read_retries: int = 2,
        write_retries: int = 0,
        max_read_registers: int = 100,
        max_read_bits: int = 256,
        max_write_registers: int = 100,
        max_write_bits: int = 256,
        auto_connect: bool = False,
        series: str | SeriesProfile = "auto",
        strict_series: bool = False,
        sync_call_timeout: float = 120.0,
    ) -> None:
        options: dict[str, Any] = {
            "port": port,
            "unit": unit,
            "timeout": timeout,
            "connect_timeout": connect_timeout,
            "read_retries": read_retries,
            "write_retries": write_retries,
            "max_read_registers": max_read_registers,
            "max_read_bits": max_read_bits,
            "max_write_registers": max_write_registers,
            "max_write_bits": max_write_bits,
            "auto_connect": auto_connect,
            "series": series,
            "strict_series": strict_series,
        }
        self.sync_call_timeout = sync_call_timeout
        self._loop_thread = _LoopThread()
        self._submission_lock = threading.Lock()
        self._closed = False
        # pymodbus captures the running loop in its constructor, so the async
        # client must be built on the loop thread, not on the caller's thread.
        self._client = self._submit_raw(lambda: AsyncDeltaPLC(host, **options)).result(
            sync_call_timeout
        )
        atexit.register(self.close)

    # -- lifecycle ---------------------------------------------------------

    @property
    def series(self) -> str | SeriesProfile:
        """Configured series parameter ('auto' or profile name/object)."""
        return self._client.series

    @property
    def strict_series(self) -> bool:
        """Whether strict series checking is enabled."""
        return self._client.strict_series

    @property
    def configured_profile(self) -> SeriesProfile | None:
        """Configured hardware profile, or None if auto."""
        return self._client.configured_profile

    @property
    def detected_profile(self) -> SeriesProfile | None:
        """Hardware profile detected from D1001, or None before connect / if probe failed."""
        return self._client.detected_profile

    @property
    def effective_profile(self) -> SeriesProfile:
        """Active profile governing valid addresses and chunk boundaries."""
        return self._client.effective_profile

    @property
    def is_connected(self) -> bool:
        """True while the underlying transport is open; needs no loop thread."""
        return self._client.is_connected

    def connect(self) -> None:
        """Open the socket, raising ``DeltaConnectionError`` on failure."""
        self._run(self._client.connect)

    def close(self) -> None:
        """Close the socket and stop the loop thread; idempotent."""
        self._check_loop_thread()
        if self._closed:
            return
        self._closed = True
        atexit.unregister(self.close)
        future = self._loop_thread.submit(self._shutdown_client())
        try:
            with contextlib.suppress(Exception):
                future.result(self.sync_call_timeout)
        finally:
            self._loop_thread.stop(self.sync_call_timeout)

    async def _shutdown_client(self) -> None:
        """Close the async client, then let its transport finish closing."""
        await self._client.close()
        await asyncio.sleep(0)

    def __enter__(self) -> DeltaPLC:  # noqa: PYI034 - Self needs 3.11
        """Connect and return this client."""
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the client on context exit."""
        self.close()

    # -- public reads ------------------------------------------------------

    def read(
        self, tag: str, count: int = 1
    ) -> int | float | bool | list[int] | list[float] | list[bool]:
        """Read *count* values from *tag* (scalar when ``count == 1``)."""
        return self._run(lambda: self._client.read(tag, count))

    def read_u16(self, tag: str, count: int = 1) -> int | list[int]:
        """Read unsigned 16-bit values from *tag*."""
        return self._run(lambda: self._client.read_u16(tag, count))

    def read_i16(self, tag: str, count: int = 1) -> int | list[int]:
        """Read signed 16-bit values from *tag*."""
        return self._run(lambda: self._client.read_i16(tag, count))

    def read_i32(self, tag: str, count: int = 1) -> int | list[int]:
        """Read signed 32-bit values from *tag* (CDAB word order)."""
        return self._run(lambda: self._client.read_i32(tag, count))

    def read_u32(self, tag: str, count: int = 1) -> int | list[int]:
        """Read unsigned 32-bit values from *tag* (CDAB word order)."""
        return self._run(lambda: self._client.read_u32(tag, count))

    def read_f32(self, tag: str, count: int = 1) -> float | list[float]:
        """Read 32-bit floats from *tag* (CDAB word order)."""
        return self._run(lambda: self._client.read_f32(tag, count))

    def read_bits(self, tag: str, count: int = 1) -> bool | list[bool]:
        """Read *count* M/X/Y wire bits (scalar when ``count == 1``)."""
        return self._run(lambda: self._client.read_bits(tag, count))

    def read_many(self, tags: Sequence[str]) -> dict[str, int | float | bool]:
        """Read every tag in *tags* into a ``{normalized tag: value}`` dict."""
        return self._run(lambda: self._client.read_many(tags))

    def read_planned(
        self, groups: Sequence[ReadGroup], *, retries: int | None = None
    ) -> list[GroupResult]:
        """Execute an existing read plan, one result per group."""
        return self._run(lambda: self._client.read_planned(groups, retries=retries))

    # -- public writes -----------------------------------------------------

    def write(self, tag: str, value: Any) -> None:
        """Write an untyped value or sequence to *tag*."""
        self._run(lambda: self._client.write(tag, value))

    def write_u16(self, tag: str, value: Any) -> None:
        """Write unsigned 16-bit values to *tag*."""
        self._run(lambda: self._client.write_u16(tag, value))

    def write_i16(self, tag: str, value: Any) -> None:
        """Write signed 16-bit values to *tag*."""
        self._run(lambda: self._client.write_i16(tag, value))

    def write_i32(self, tag: str, value: Any) -> None:
        """Write signed 32-bit values to *tag* (CDAB word order)."""
        self._run(lambda: self._client.write_i32(tag, value))

    def write_u32(self, tag: str, value: Any) -> None:
        """Write unsigned 32-bit values to *tag* (CDAB word order)."""
        self._run(lambda: self._client.write_u32(tag, value))

    def write_f32(self, tag: str, value: Any) -> None:
        """Write 32-bit floats to *tag* (CDAB word order)."""
        self._run(lambda: self._client.write_f32(tag, value))

    def write_bits(self, tag: str, value: Any) -> None:
        """Write one or more M/X/Y wire bits."""
        self._run(lambda: self._client.write_bits(tag, value))

    # -- internals ---------------------------------------------------------

    def _run(self, factory: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
        """Build *factory*'s coroutine after the loop-thread check and block.

        Creating the coroutine before the check would leave it un-awaited when
        the check raises.
        """
        self._check_loop_thread()
        with self._submission_lock:
            if self._closed:
                raise RuntimeError("DeltaPLC is closed")
            future = self._loop_thread.submit(factory())
            try:
                return future.result(self.sync_call_timeout)
            except (concurrent.futures.TimeoutError, TimeoutError) as exc:
                future.cancel()
                raise DeltaTimeoutError(
                    f"sync call timed out after {self.sync_call_timeout}s"
                ) from exc

    def _check_loop_thread(self) -> None:
        """Reject calls made from the loop thread, which would deadlock."""
        thread_id = self._loop_thread.thread_id
        if thread_id is not None and threading.get_ident() == thread_id:
            raise RuntimeError(_THREAD_ERROR)

    def _submit_raw(self, fn: Callable[[], Any]) -> Future[Any]:
        """Run a plain callable on the loop thread (test seam)."""

        async def runner() -> Any:
            return fn()

        return self._loop_thread.submit(runner())
