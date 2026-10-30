"""Hardware-faithful Delta DVP mock server used by tests, the CLI, and doctor.

The mock reproduces the bench quirks the driver has to survive: the DVP block
layout (D0-D4095 / D4096-D11999, M0-M1535 / M1536-M4095), the octal X/Y bit
banks, the read-only guards (system, scan and fault registers), and the PDU
count limits of a real DVP.  pymodbus' own simulator is used for storage, but
its permissive handling of undefined addresses (bit blocks make every gap
between segments addressable) and of `readonly` bit blocks is corrected by a
request handler that pre-checks every request.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any, cast

from pymodbus.constants import ExcCodes
from pymodbus.pdu import ExceptionResponse, ModbusPDU
from pymodbus.server import ModbusTcpServer
from pymodbus.server.requesthandler import ServerRequestHandler
from pymodbus.simulator import DataType, SimData, SimDevice

from .addressing import Device, parse_tag, wire_for
from .dvp_map import (
    D_COUNT,
    D_SEGMENT_SPANS,
    D_WIRE0,
    M_COUNT,
    M_SEGMENT_SPANS,
    PDU_CAPS,
    READ_RANGES,
    WRITE_GUARDS,
    XY_COUNT,
    function_code_for,
)

__all__ = ["MockServer", "MockStats"]

# --------------------------------------------------------------------------- wire map

_WRITE_FCS = frozenset({5, 6, 15, 16})

#: Scan-time model for `live_scan`: base 1 ms plus deterministic triangular jitter.
_SCAN_BASE_MS = 1.0
_SCAN_PEAK_MS = 20.0
_SCAN_REG_INDEX = 1010  # D1010-D1012 inside the hr block starting at 0x1000

_SimAction = Callable[
    [int, int, int, int, list[int], list[int] | list[bool] | None],
    Coroutine[Any, Any, ExcCodes | None],
]


@dataclass
class MockStats:
    """Request counters exposed by :class:`MockServer`."""

    requests: int = 0
    writes: int = 0
    rejected: int = 0
    clients: int = 0


# --------------------------------------------------------------------------- device


def _overlaps(spans: tuple[tuple[int, int], ...], start: int, count: int) -> bool:
    """True when the inclusive span ``[start, start + count - 1]`` hits any span."""
    end = start + count - 1
    return any(start <= hi and end >= lo for lo, hi in spans)


def _within(ranges: tuple[tuple[int, int], ...], start: int, count: int) -> bool:
    """True when the whole inclusive span fits inside one of ``ranges``."""
    end = start + count - 1
    return any(lo <= start and end <= hi for lo, hi in ranges)


def _scan_ms(t0: float) -> int:
    """Deterministic scan time in ms: 1 ms base plus a 20 ms triangular wave."""
    elapsed = time.monotonic() - t0
    half_cycle = _SCAN_PEAK_MS / 1000.0
    phase = (elapsed % (2 * half_cycle)) / half_cycle
    ramp = phase if phase <= 1 else 2 - phase
    return max(1, round(_SCAN_BASE_MS + ramp * (_SCAN_PEAK_MS - _SCAN_BASE_MS)))


def _make_scan_action(t0: float) -> _SimAction:
    """Build the simulator action that keeps D1010-D1012 moving while serving."""

    async def action(
        function_code: int,
        start_address: int,
        address: int,
        count: int,
        registers: list[int],
        values: list[int] | list[bool] | None,
    ) -> ExcCodes | None:
        del function_code, address, count, values
        if start_address == D_WIRE0:
            scan = _scan_ms(t0)
            registers[_SCAN_REG_INDEX : _SCAN_REG_INDEX + 3] = [scan, scan, scan]
        return None

    return action


def _default_values() -> dict[Device, list[Any]]:
    """Full-index value table with the bench defaults applied.

    ``D1000=200, D1001=0x7C21, D1002=15872`` (firmware id), ``D1010=4,
    D1011=1, D1012=12`` (scan time), ``D1121=1`` (station id), ``M1000=True``
    (run flag); the fault code ``D1067=0`` and fault flag ``M1067=False`` are
    the zero defaults.
    """
    values: dict[Device, list[Any]] = {
        Device.D: [0] * D_COUNT,
        Device.M: [False] * M_COUNT,
        Device.X: [False] * XY_COUNT,
        Device.Y: [False] * XY_COUNT,
    }
    values[Device.D][1000:1003] = [200, 0x7C21, 15872]  # firmware id
    values[Device.D][1010:1013] = [4, 1, 12]  # scan time
    values[Device.D][1121] = 1  # station id
    values[Device.M][1000] = True  # run flag
    # D1067 fault code and M1067 fault flag already default to 0/False.
    return values


def _overlay_seed(values: dict[Device, list[Any]], seed: Mapping[str, int | bool]) -> None:
    """Apply user seeds, rejecting tags the bench cannot seed."""
    for key, value in seed.items():
        parsed = parse_tag(key)
        address = parsed.address
        if parsed.dtype is not None or address.bit is not None:
            raise ValueError(f"seed {key!r}: use a bare tag without bit or dtype suffix")
        if address.device is Device.D:
            if not isinstance(value, int) or not 0 <= value <= 0xFFFF:
                raise ValueError(f"seed {key!r}: D values must be integers in 0..65535")
        elif not isinstance(value, bool):
            raise ValueError(f"seed {key!r}: M/X/Y values must be booleans")
        values[address.device][address.index] = value


def _segments(
    device: Device,
    values: list[Any],
    spans: tuple[tuple[int, int, bool], ...],
    datatype: DataType,
) -> list[SimData]:
    """One SimData per contiguous span, addressed by wire number."""
    return [
        SimData(
            address=wire_for(device, lo),
            count=1,
            values=list(values[lo:hi]),
            datatype=datatype,
            readonly=readonly,
        )
        for lo, hi, readonly in spans
    ]


def _build_device(
    seed: Mapping[str, int | bool] | None = None,
    *,
    fault_code: int | None = None,
    plc_stop: bool = False,
    fixed_scan_ms: float | None = None,
    station_id: int | None = None,
    action: _SimAction | None = None,
) -> SimDevice:
    """Build the single wildcard-unit (id 0) DVP device.

    Defaults, seeds, then the `fault_code` / `plc_stop` / `fixed_scan_ms`
    overlays are applied to the value tables before the `SimData` blocks are
    built.  ``co`` and ``di`` are sorted by wire address because pymodbus
    bit-packs them starting at ``simdata[0].address``.
    """
    values = _default_values()
    if seed:
        _overlay_seed(values, seed)
    if fault_code is not None:
        values[Device.D][1067] = fault_code
        values[Device.M][1067] = True
    if plc_stop:
        values[Device.M][1000] = False
    if fixed_scan_ms is not None:
        scan = round(fixed_scan_ms * 10)
        values[Device.D][1010:1013] = [scan, scan, scan]
    if station_id is not None:
        if not (0 <= station_id <= 65535):
            raise ValueError(f"station_id {station_id} out of range 0..65535")
        values[Device.D][1121] = station_id

    co = _segments(Device.M, values[Device.M], M_SEGMENT_SPANS, DataType.BITS)
    co += _segments(Device.Y, values[Device.Y], ((0, XY_COUNT, False),), DataType.BITS)
    co.sort(key=lambda entry: entry.address)
    di = _segments(Device.X, values[Device.X], ((0, XY_COUNT, True),), DataType.BITS)
    hr = _segments(Device.D, values[Device.D], D_SEGMENT_SPANS, DataType.REGISTERS)
    hr.sort(key=lambda entry: entry.address)
    ir = [SimData(address=0, count=1, datatype=DataType.INVALID)]

    # A tuple simdata forces pymodbus into per-bit (wire) addressing.
    return SimDevice(id=0, simdata=(co, di, hr, ir), action=action)


# --------------------------------------------------------------------------- server


class _MockHandler(ServerRequestHandler):
    """Per-connection handler that enforces the DVP caps, ranges, and guards."""

    def __init__(
        self,
        owner: ModbusTcpServer,
        stats: MockStats,
        *,
        reject: bool = False,
        counted: bool = False,
    ) -> None:
        super().__init__(owner, owner.trace_packet, owner.trace_pdu, owner.trace_connect)
        self.stats = stats
        self.reject = reject
        self.counted = counted

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Accept the connection, or drop it when the client limit is reached."""
        super().connection_made(transport)
        if self.reject:
            transport.close()

    def callback_disconnected(self, exc: Exception | None) -> None:
        """Release the client slot once, then let pymodbus clean up."""
        if self.counted:
            self.counted = False
            self.stats.clients -= 1
        super().callback_disconnected(exc)

    @staticmethod
    def _reject_code(fc: int, address: int, count: int) -> ExcCodes | None:
        """First reason to refuse the request, exactly as the bench would."""
        cap = PDU_CAPS.get(fc)
        if cap is not None and count > cap:
            return ExcCodes.ILLEGAL_VALUE
        ranges = READ_RANGES.get(fc)
        if ranges is not None and not _within(ranges, address, count):
            return ExcCodes.ILLEGAL_ADDRESS
        guards = WRITE_GUARDS.get(fc)
        if guards is not None and _overlaps(guards, address, count):
            return ExcCodes.ILLEGAL_ADDRESS
        return None

    def _send_exception(self, pdu: ModbusPDU, fc: int, code: ExcCodes) -> None:
        """Answer `pdu` with the same exception path the base handler uses."""
        self.stats.rejected += 1
        response = ExceptionResponse(fc, exception_code=code)
        response.transaction_id = pdu.transaction_id
        response.dev_id = pdu.dev_id
        self.server_send(response, self.last_addr)

    async def handle_request(self) -> None:
        """Pre-check the request, then defer to the simulator datastore."""
        pdu = self.last_pdu
        if pdu is None:
            await super().handle_request()
            return
        self.stats.requests += 1
        fc = pdu.function_code
        count = pdu.count if pdu.count > 0 else 1  # single-coil PDUs leave count=0
        code = self._reject_code(fc, pdu.address, count)
        if code is not None:
            self._send_exception(pdu, fc, code)
            return
        if fc in _WRITE_FCS:
            self.stats.writes += 1
        await super().handle_request()


class _MockTcpServer(ModbusTcpServer):
    """Modbus TCP server that counts clients and refuses the excess ones."""

    def __init__(
        self,
        context: SimDevice,
        *,
        stats: MockStats,
        max_clients: int,
        **kwargs: Any,
    ) -> None:
        super().__init__(context, **kwargs)
        self.stats = stats
        self.max_clients = max_clients

    def callback_new_connection(self) -> _MockHandler:
        """Return a handler, refusing connections past ``max_clients``."""
        if self.stats.clients >= self.max_clients:
            return _MockHandler(self, self.stats, reject=True, counted=False)
        self.stats.clients += 1
        return _MockHandler(self, self.stats, reject=False, counted=True)


class MockServer:
    """Hardware-faithful Delta DVP Modbus TCP mock.

    ``port=0`` picks a free port; read :attr:`address` after :meth:`start` to
    learn which one.  :meth:`serve` blocks until :meth:`stop` (what the CLI
    needs), everything else is non-blocking.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5020,
        *,
        max_clients: int = 4,
        seed: Mapping[str, int | bool] | None = None,
        fault_code: int | None = None,
        plc_stop: bool = False,
        fixed_scan_ms: float | None = None,
        live_scan: bool = False,
        station_id: int | None = None,
    ) -> None:
        if fixed_scan_ms is not None and live_scan:
            raise ValueError("fixed_scan_ms and live_scan are mutually exclusive")
        self.host = host
        self.port = port
        self.max_clients = max_clients
        self.stats = MockStats()
        self.address: tuple[str, int] = (host, port)
        self._action = _make_scan_action(time.monotonic()) if live_scan else None
        self._device = _build_device(
            seed=seed,
            fault_code=fault_code,
            plc_stop=plc_stop,
            fixed_scan_ms=fixed_scan_ms,
            station_id=station_id,
            action=self._action,
        )
        self._stop_event = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: _MockTcpServer | None = None
        self._startup_error: BaseException | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        """True while the listener thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def _thread_main(self) -> None:
        """Bind the listener, publish the address, then serve until stopped."""
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve_forever())
        except BaseException as exc:  # noqa: BLE001 - re-raised from start()
            self._startup_error = exc
        self._ready.set()
        if self._startup_error is None:
            loop.run_forever()
        loop.close()

    async def _serve_forever(self) -> None:
        """Bind the socket and return; the caller runs the loop."""
        self._server = _MockTcpServer(
            self._device,
            stats=self.stats,
            max_clients=self.max_clients,
            address=(self.host, self.port),
        )
        await self._server.serve_forever(background=True)
        # In pymodbus 3.16 `transport` is the asyncio Server, not a socket transport.
        listener = cast("asyncio.Server", self._server.transport)
        self.address = listener.sockets[0].getsockname()[:2]

    def start(self) -> None:
        """Bind the listener and return; does not block."""
        if self.running:
            return
        self._stop_event.clear()
        self._ready = threading.Event()
        self._startup_error = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._thread_main, name="delta-plc-mock", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(5):
            self.stop()
            raise RuntimeError("mock server did not start within 5 s")
        if self._startup_error is not None:
            self.stop()
            raise self._startup_error

    def serve(self) -> None:
        """Start if needed, then block until :meth:`stop`."""
        self.start()
        self._stop_event.wait()

    def stop(self) -> None:
        """Shut the listener down; safe to call more than once."""
        if not self.running:
            self._stop_event.set()
            return
        loop, server = self._loop, self._server
        if loop is not None and server is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(server.shutdown(), loop).result(5)
        if loop is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(5)
        self._thread = None
        self._loop = None
        self._server = None
        self._stop_event.set()

    # -- runtime values ----------------------------------------------------

    def set_values(self, tag: str, value: int | bool) -> None:
        """Overwrite one mock value in place, bypassing the request path.

        Used by differential tests to seed values without consuming request
        counters.  The simulator's own read-only flags still refuse the guarded
        D register spans (D1000-D1002, D1010-D1012, D1067), which stay
        unchanged; bit guards are not enforced on this direct path.
        """
        if not self.running or self._loop is None or self._server is None:
            raise RuntimeError("mock server is not running")
        parsed = parse_tag(tag)
        address = parsed.address
        if parsed.dtype is not None or address.bit is not None:
            raise ValueError(f"set_values {tag!r}: use a bare tag without bit or dtype suffix")
        raw: list[int] | list[bool]
        if address.device is Device.D:
            if not isinstance(value, int) or not 0 <= value <= 0xFFFF:
                raise ValueError(f"set_values {tag!r}: D values must be integers in 0..65535")
            raw = [int(value)]
        else:
            raw = [bool(value)]
        fc = function_code_for(address.device)
        future = asyncio.run_coroutine_threadsafe(
            self._server.context.async_setValues(0, fc, address.wire, raw),
            self._loop,
        )
        future.result(timeout=5)
