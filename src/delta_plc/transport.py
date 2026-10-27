"""Async Delta DVP transport: connection, reads, retries, and quality results.

One :class:`asyncio.Lock` serializes every request on an instance, so a client
uses exactly one socket and never pipelines. Public operations acquire the lock
once and then call lock-free internals; that is what makes nesting safe.

Reads are planned into coalesced :class:`ReadGroup` objects (see ``planning``)
and executed group by group. A short (truncated) response is a protocol error
and is never padded or retried; only transport and timeout failures are retried,
and a connection/timeout failure stops the rest of the plan early.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException

from .addressing import Device
from .errors import (
    DeltaConnectionError,
    DeltaModbusException,
    DeltaPLCError,
    DeltaProtocolError,
    DeltaTimeoutError,
    DeltaValueError,
    NotConnectedError,
)
from .packing import DType, decode_value, decode_values, get_bit
from .planning import ReadGroup, Tag, make_tag, make_tags, plan_chunks, plan_reads

__all__ = ["AsyncDeltaPLC", "GroupResult", "decode_group_values"]

#: Modbus read function per Delta device. X and Y are bit devices like M.
_FUNCTION_FOR: dict[Device, int] = {
    Device.D: 3,  # holding registers
    Device.X: 2,  # discrete inputs
    Device.Y: 1,  # coils
    Device.M: 1,  # coils
}

#: pymodbus method name per read function code.
_METHOD_FOR: dict[int, str] = {
    3: "read_holding_registers",
    1: "read_coils",
    2: "read_discrete_inputs",
}

#: Backoff between read retries; the last value repeats.
_RETRY_DELAYS: tuple[float, ...] = (0.2, 0.5, 1.0)

#: Errors worth another attempt: the PLC may simply not have answered in time.
_RETRYABLE = (DeltaConnectionError, DeltaTimeoutError)


@dataclass
class GroupResult:
    """One executed read group plus its decoded transport outcome.

    ``registers`` and ``bits`` are the raw words and wire bits the PLC returned
    (exactly ``group.count`` of them) and are empty when ``error`` is set.
    ``elapsed_ms`` is the successful attempt's request time in milliseconds.
    """

    group: ReadGroup
    registers: list[int] = field(default_factory=list)
    bits: list[bool] = field(default_factory=list)
    error: DeltaPLCError | None = None
    elapsed_ms: float = 0.0


def decode_group_values(
    group: ReadGroup,
    registers: Sequence[int],
    bits: Sequence[bool],
) -> dict[str, int | float | bool]:
    """Decode one group's raw response into ``{tag.name: value}``.

    Preserves ``group.tags`` order. D bit tags read their bit out of the word
    at their offset; M/X/Y bit tags take the wire bit; every other tag decodes
    ``tag.width`` registers starting at its offset.
    """
    values: dict[str, int | float | bool] = {}
    for tag in group.tags:
        offset = tag.address.wire - group.start
        if tag.dtype is DType.BIT:
            if tag.address.device is Device.D:
                bit = tag.address.bit
                assert bit is not None
                values[tag.name] = get_bit(registers[offset], bit)
            else:
                values[tag.name] = bool(bits[offset])
        else:
            values[tag.name] = decode_value(
                registers[offset : offset + tag.width], tag.dtype
            )
    return values


def _function_for(tag: Tag) -> int:
    """Read function code for *tag*'s device."""
    return _FUNCTION_FOR[tag.address.device]


class AsyncDeltaPLC:
    """Async Delta DVP client: one lock, one socket, retry-aware reads."""

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
    ) -> None:
        self.host = host
        self.port = port
        self.unit = unit
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.read_retries = read_retries
        self.write_retries = write_retries
        self.max_read_registers = max_read_registers
        self.max_read_bits = max_read_bits
        self.max_write_registers = max_write_registers
        self.max_write_bits = max_write_bits
        self.auto_connect = auto_connect
        # The driver owns retry and reconnect policy, so pymodbus defaults are off.
        self._client = AsyncModbusTcpClient(
            host, port=port, timeout=timeout, retries=0, reconnect_delay=0
        )
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """True while the underlying transport is open."""
        return bool(self._client is not None and self._client.connected)

    async def connect(self) -> None:
        """Open the socket, raising ``DeltaConnectionError`` on failure."""
        async with self._lock:
            await self._connect_locked()

    async def close(self) -> None:
        """Close the socket; idempotent."""
        async with self._lock:
            self._client.close()

    async def __aenter__(self) -> AsyncDeltaPLC:  # noqa: PYI034 - Self needs 3.11
        """Connect and return this client."""
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the client on context exit."""
        await self.close()

    async def _connect_locked(self) -> None:
        """Connect without the lock; the caller must already hold it."""
        try:
            connected = await asyncio.wait_for(
                self._client.connect(), self.connect_timeout
            )
        except (asyncio.TimeoutError, OSError) as exc:
            raise DeltaConnectionError(
                f"could not connect to {self.host}:{self.port}: {exc}"
            ) from exc
        if not connected:
            raise DeltaConnectionError(f"could not connect to {self.host}:{self.port}")

    async def _ensure_connected_locked(self) -> None:
        """Require a connection, or make one when ``auto_connect`` is set."""
        if self.is_connected:
            return
        if not self.auto_connect:
            raise NotConnectedError(
                f"{self.host}:{self.port} is not connected; call connect() first"
            )
        await self._connect_locked()

    # -- public reads ------------------------------------------------------

    async def read(
        self, tag: str, count: int = 1
    ) -> int | float | bool | list[int] | list[float] | list[bool]:
        """Read *count* values from *tag* (scalar when ``count == 1``)."""
        return await self._read_typed(tag, None, count)

    async def read_u16(self, tag: str, count: int = 1) -> int | list[int]:
        """Read unsigned 16-bit values from *tag*."""
        return await self._read_typed(tag, DType.U16, count)

    async def read_i16(self, tag: str, count: int = 1) -> int | list[int]:
        """Read signed 16-bit values from *tag*."""
        return await self._read_typed(tag, DType.I16, count)

    async def read_i32(self, tag: str, count: int = 1) -> int | list[int]:
        """Read signed 32-bit values from *tag* (CDAB word order)."""
        return await self._read_typed(tag, DType.I32, count)

    async def read_u32(self, tag: str, count: int = 1) -> int | list[int]:
        """Read unsigned 32-bit values from *tag* (CDAB word order)."""
        return await self._read_typed(tag, DType.U32, count)

    async def read_f32(self, tag: str, count: int = 1) -> float | list[float]:
        """Read 32-bit floats from *tag* (CDAB word order)."""
        return await self._read_typed(tag, DType.F32, count)

    async def read_bits(self, tag: str, count: int = 1) -> bool | list[bool]:
        """Read *count* M/X/Y wire bits (scalar when ``count == 1``)."""
        parsed = make_tag(tag, DType.BIT)
        if parsed.address.device is Device.D:
            raise DeltaValueError(
                f"read_bits supports only M/X/Y tags; read {tag!r} as a word bit "
                "with read('D<n>.<bit>') instead"
            )
        bits = await self._read_wire_bits(parsed, count)
        return bits[0] if count == 1 else bits

    async def read_many(self, tags: Sequence[str]) -> dict[str, int | float | bool]:
        """Read every tag in *tags*, coalescing them into one plan.

        Keys are the normalized tag names in the requested order. Duplicate
        normalized keys are rejected before any I/O; the first group error is
        raised after the plan runs.
        """
        parsed = make_tags(tags)
        names = [tag.name for tag in parsed]
        seen: set[str] = set()
        for name in names:
            if name in seen:
                raise DeltaValueError(f"duplicate tag {name!r} in read_many")
            seen.add(name)
        groups = plan_reads(
            parsed, max_registers=self.max_read_registers, max_bits=self.max_read_bits
        )
        async with self._lock:
            await self._ensure_connected_locked()
            results = await self._read_planned_locked(groups, self.read_retries)
        for result in results:
            if result.error is not None:
                raise result.error
        decoded: dict[str, int | float | bool] = {}
        for result in results:
            decoded.update(
                decode_group_values(result.group, result.registers, result.bits)
            )
        return {name: decoded[name] for name in names}

    async def read_planned(
        self, groups: Sequence[ReadGroup], *, retries: int | None = None
    ) -> list[GroupResult]:
        """Execute an existing read plan, one :class:`GroupResult` per group."""
        attempts = self.read_retries if retries is None else retries
        async with self._lock:
            await self._ensure_connected_locked()
            return await self._read_planned_locked(groups, attempts)

    # -- read internals (lock-free) ----------------------------------------

    async def _read_typed(self, tag: str, dtype: DType | None, count: int) -> Any:
        """Shared body of ``read`` and the typed reads; validates before I/O."""
        parsed = make_tag(tag, dtype)
        if parsed.dtype is DType.BIT:
            if count != 1:
                raise DeltaValueError(
                    f"{parsed.name} is a bit tag: count must be 1, got {count}"
                )
            return await self._read_bit(parsed)
        if count < 1:
            raise DeltaValueError(f"count must be at least 1, got {count}")
        values = await self._read_numeric(parsed, count)
        return values[0] if count == 1 else values

    async def _read_bit(self, tag: Tag) -> bool:
        """Locked read of one bit: a D word bit or an M/X/Y wire bit."""
        async with self._lock:
            await self._ensure_connected_locked()
            group = ReadGroup(_function_for(tag), tag.address.wire, 1, ())
            registers, bits, _ = await self._read_group_locked(group, self.read_retries)
        if tag.address.device is Device.D:
            bit = tag.address.bit
            assert bit is not None
            return get_bit(registers[0], bit)
        return bool(bits[0])

    async def _read_numeric(self, tag: Tag, count: int) -> list[int | float]:
        """Locked read of *count* numeric values, chunked across PDUs."""
        chunks = plan_chunks(
            tag.address,
            tag.width,
            count,
            max_registers=self.max_read_registers,
            max_bits=self.max_read_bits,
        )
        function = _function_for(tag)
        values: list[int | float] = []
        async with self._lock:
            await self._ensure_connected_locked()
            for chunk in chunks:
                group = ReadGroup(function, chunk.start, chunk.count, ())
                registers, _, _ = await self._read_group_locked(
                    group, self.read_retries
                )
                values.extend(decode_values(registers, tag.dtype))
        return values

    async def _read_wire_bits(self, tag: Tag, count: int) -> list[bool]:
        """Locked read of *count* M/X/Y wire bits, chunked across PDUs."""
        chunks = plan_chunks(
            tag.address,
            1,
            count,
            max_registers=self.max_read_registers,
            max_bits=self.max_read_bits,
        )
        function = _function_for(tag)
        out: list[bool] = []
        async with self._lock:
            await self._ensure_connected_locked()
            for chunk in chunks:
                group = ReadGroup(function, chunk.start, chunk.count, ())
                _, bits, _ = await self._read_group_locked(group, self.read_retries)
                out.extend(bits)
        return out

    async def _read_planned_locked(
        self, groups: Sequence[ReadGroup], retries: int
    ) -> list[GroupResult]:
        """Run *groups* in order without the lock; callers must hold it.

        A non-connection error marks just its own group. A connection or
        timeout failure marks its group and every remaining group, and stops
        issuing PDUs.
        """
        results: list[GroupResult] = []
        stop: DeltaPLCError | None = None
        for group in groups:
            if stop is not None:
                results.append(GroupResult(group, error=stop))
                continue
            try:
                registers, bits, elapsed_ms = await self._read_group_locked(
                    group, retries
                )
            except _RETRYABLE as exc:
                stop = exc
                results.append(GroupResult(group, error=exc))
            except DeltaPLCError as exc:
                results.append(GroupResult(group, error=exc))
            else:
                results.append(
                    GroupResult(
                        group,
                        registers=registers,
                        bits=bits,
                        error=None,
                        elapsed_ms=elapsed_ms,
                    )
                )
        return results

    async def _read_group_locked(
        self, group: ReadGroup, retries: int
    ) -> tuple[list[int], list[bool], float]:
        """Read one group with up to *retries* extra attempts; or raise."""
        attempt = 0
        while True:
            try:
                return await self._read_once(group)
            except _RETRYABLE:
                if attempt >= retries:
                    raise
                if not self.is_connected:
                    # One reconnect before the next attempt; a failure here is
                    # simply reported by that attempt.
                    try:
                        await self._connect_locked()
                    except DeltaPLCError:
                        pass
                await asyncio.sleep(
                    _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                )
                attempt += 1

    async def _read_once(self, group: ReadGroup) -> tuple[list[int], list[bool], float]:
        """Issue one read PDU and return raw ``(registers, bits, elapsed_ms)``.

        Errors are mapped to the driver hierarchy: timeouts to
        ``DeltaTimeoutError``, dropped connections to ``DeltaConnectionError``,
        anything else Modbus-shaped to ``DeltaProtocolError``, and a Modbus
        exception response to ``DeltaModbusException``. A response shorter than
        the requested count is a protocol error and is never padded.
        """
        try:
            method_name = _METHOD_FOR[group.function]
        except KeyError:
            raise DeltaProtocolError(
                f"unsupported read function code {group.function}"
            ) from None
        start = time.perf_counter()
        try:
            response = await getattr(self._client, method_name)(
                group.start, count=group.count, device_id=self.unit
            )
        except ModbusIOException as exc:
            raise DeltaTimeoutError(f"{group.function}@{group.start}: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise DeltaTimeoutError(f"{group.function}@{group.start}: {exc}") from exc
        except ConnectionException as exc:
            raise DeltaConnectionError(f"{group.function}@{group.start}: {exc}") from exc
        except OSError as exc:
            raise DeltaConnectionError(f"{group.function}@{group.start}: {exc}") from exc
        except ModbusException as exc:
            raise DeltaProtocolError(f"{group.function}@{group.start}: {exc}") from exc
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        if response.isError():
            function = getattr(response, "function_code", group.function) & 0x7F
            code = getattr(response, "exception_code", 0)
            raise DeltaModbusException(function, int(code))

        if group.function == 3:
            registers = [int(word) for word in getattr(response, "registers", ())]
            bits: list[bool] = []
            received = len(registers)
        else:
            registers = []
            bits = [bool(bit) for bit in getattr(response, "bits", ())][: group.count]
            received = len(bits)
        if received < group.count:
            raise DeltaProtocolError(
                f"{group.function}@{group.start}: expected {group.count} values, "
                f"got {received}"
            )
        return registers[: group.count], bits, elapsed_ms
