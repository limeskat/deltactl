"""Async Delta DVP transport: connection, reads, writes, retries, quality.

One :class:`asyncio.Lock` serializes every request on an instance, so a client
uses exactly one socket and never pipelines. Public operations acquire the lock
once and then call lock-free internals; that is what makes nesting safe.

Reads are planned into coalesced :class:`ReadGroup` objects (see ``planning``)
and executed group by group. Writes validate their whole span before the lock
is taken, then chunk by PDU and by memory block. A short (truncated) response
is a protocol error and is never padded or retried; only transport and timeout
failures are retried, and a timed-out write closes its socket so a late reply
can never be matched to a later request.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException

from .addressing import Address, Device, validate_span
from .errors import (
    DeltaConnectionError,
    DeltaModbusException,
    DeltaPLCError,
    DeltaProtocolError,
    DeltaTimeoutError,
    DeltaValueError,
    NotConnectedError,
)
from .packing import (
    DType,
    decode_value,
    decode_values,
    encode_value,
    encode_values,
    get_bit,
    mask_u16,
    set_bit,
)
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

#: pymodbus method name per write function code.
_WRITE_METHOD_FOR: dict[int, str] = {
    6: "write_register",
    16: "write_registers",
    5: "write_coil",
    15: "write_coils",
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
            values[tag.name] = decode_value(registers[offset : offset + tag.width], tag.dtype)
    return values


def _function_for(tag: Tag) -> int:
    """Read function code for *tag*'s device."""
    return _FUNCTION_FOR[tag.address.device]


def _as_bit(value: Any) -> bool:
    """Coerce ``bool``/``0``/``1`` to a real ``bool``; else ``DeltaValueError``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise DeltaValueError(f"{value!r} is not a valid bit value; expected bool, 0, or 1")


def _wire_bits(value: Any) -> list[bool]:
    """Coerce a bit or an iterable of bits into a list of ``bool``."""
    if isinstance(value, (bool, int)):
        return [_as_bit(value)]
    try:
        return [_as_bit(item) for item in value]
    except TypeError:
        raise DeltaValueError(f"{value!r} is not a valid bit value or sequence of bits") from None


def _masked_values(value: Any) -> list[int]:
    """Mask a plain-D scalar or sequence into 16-bit registers.

    ``bool`` is rejected (``bool`` is an ``int`` subclass but not a register
    value); use a ``D<n>.<bit>`` tag or ``write_bits`` for bits.
    """
    if isinstance(value, bool):
        raise DeltaValueError(
            "bool is not a valid 16-bit value; use a D<n>.<bit> tag for word bits"
        )
    if isinstance(value, int):
        return [mask_u16(value)]
    try:
        items = list(value)
    except TypeError:
        raise DeltaValueError(f"{value!r} is not a valid 16-bit value") from None
    return [_mask_element(item) for item in items]


def _mask_element(value: Any) -> int:
    """Mask one element of a plain-D write sequence."""
    if isinstance(value, bool):
        raise DeltaValueError("bool is not a valid 16-bit value in a sequence")
    return mask_u16(value)


def _encode_write(value: Any, dtype: DType) -> list[int]:
    """Encode a scalar or sequence of *dtype* values into flat registers."""
    if isinstance(value, (bool, int, float)):
        return encode_value(value, dtype)
    try:
        seq = list(value)
    except TypeError:
        raise DeltaValueError(f"{value!r} is not a valid {dtype.value} value") from None
    return encode_values(seq, dtype)


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
            connected = await asyncio.wait_for(self._client.connect(), self.connect_timeout)
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
            decoded.update(decode_group_values(result.group, result.registers, result.bits))
        return {name: decoded[name] for name in names}

    async def read_planned(
        self, groups: Sequence[ReadGroup], *, retries: int | None = None
    ) -> list[GroupResult]:
        """Execute an existing read plan, one :class:`GroupResult` per group."""
        attempts = self.read_retries if retries is None else retries
        async with self._lock:
            await self._ensure_connected_locked()
            return await self._read_planned_locked(groups, attempts)

    # -- public writes -----------------------------------------------------

    async def write(self, tag: str, value: Any) -> None:
        """Write an untyped value or sequence to *tag*.

        M/X/Y and ``D<n>.<bit>`` take ``bool``/``0``/``1``; a D word takes
        ``mask_u16`` scalars or sequences. A ``bool`` on a plain D tag is a
        ``DeltaValueError`` (use a bit tag or ``write_bits``).
        """
        await self._write_untyped(tag, value)

    async def write_u16(self, tag: str, value: Any) -> None:
        """Write unsigned 16-bit values to *tag*."""
        await self._write_typed(tag, DType.U16, value)

    async def write_i16(self, tag: str, value: Any) -> None:
        """Write signed 16-bit values to *tag*."""
        await self._write_typed(tag, DType.I16, value)

    async def write_i32(self, tag: str, value: Any) -> None:
        """Write signed 32-bit values to *tag* (CDAB word order)."""
        await self._write_typed(tag, DType.I32, value)

    async def write_u32(self, tag: str, value: Any) -> None:
        """Write unsigned 32-bit values to *tag* (CDAB word order)."""
        await self._write_typed(tag, DType.U32, value)

    async def write_f32(self, tag: str, value: Any) -> None:
        """Write 32-bit floats to *tag* (CDAB word order)."""
        await self._write_typed(tag, DType.F32, value)

    async def write_bits(self, tag: str, value: Any) -> None:
        """Write one or more M/X/Y wire bits; D tags raise ``DeltaValueError``."""
        parsed = make_tag(tag, DType.BIT)
        if parsed.address.device is Device.D:
            raise DeltaValueError(
                f"write_bits supports only M/X/Y tags; write {tag!r} as a word bit "
                "with write('D<n>.<bit>', value) instead"
            )
        await self._guard_and_write(parsed, _wire_bits(value))

    # -- write internals (validation is done before the lock) --------------

    async def _write_untyped(self, tag: str, value: Any) -> None:
        """Shared body of ``write``: bit tags take bits, D values mask or encode.

        A plain D tag (or an inline ``:u16``) masks through ``mask_u16``; an
        inline ``:i16``/``:i32``/``:u32``/``:f32`` tag encodes through
        ``packing`` like the typed writes.
        """
        parsed = make_tag(tag)
        values: list[Any]
        if parsed.dtype is DType.BIT:
            if parsed.address.device is Device.D:
                values = [_as_bit(value)]
            else:
                values = _wire_bits(value)
        elif parsed.dtype is DType.U16:
            values = _masked_values(value)
        else:
            values = _encode_write(value, parsed.dtype)
        await self._guard_and_write(parsed, values)

    async def _write_typed(self, tag: str, dtype: DType, value: Any) -> None:
        """Shared body of the typed writes; encodes through ``packing``."""
        parsed = make_tag(tag, dtype)
        await self._guard_and_write(parsed, _encode_write(value, parsed.dtype))

    async def _guard_and_write(self, tag: Tag, values: list[Any]) -> None:
        """Validate the full write span, then write under one lock.

        The span check runs before the lock is taken, so every guard failure
        (range, boundary, read-only) happens without sending any PDU.
        """
        if not values:
            raise DeltaValueError("write requires at least one value")
        bit_like = tag.dtype is DType.BIT
        count = len(values) if bit_like else len(values) // tag.width
        total = count * tag.width if tag.address.device is Device.D else count
        validate_span(tag.address, total, write=True, allow_block_crossing=True)
        async with self._lock:
            await self._ensure_connected_locked()
            await self._write_locked(tag, values)

    async def _write_locked(self, tag: Tag, values: list[Any]) -> None:
        """Write *values* for *tag* without the lock; callers must hold it.

        ``values`` is the flat register list for a D word tag or the list of
        wire bits for M/X/Y. Chunks come from ``plan_chunks``, so a single
        32-bit value that straddles a split raises :class:`BlockBoundaryError`
        before any PDU and bulk writes split into whole-value chunks.
        """
        address = tag.address
        if address.device is Device.D and tag.dtype is DType.BIT:
            await self._write_word_bit_locked(address, bool(values[0]))
        elif address.device is Device.D:
            chunks = plan_chunks(
                address,
                tag.width,
                len(values) // tag.width,
                max_registers=self.max_write_registers,
                max_bits=self.max_write_bits,
            )
            for chunk in chunks:
                registers = values[
                    chunk.first * tag.width : (chunk.first + chunk.values) * tag.width
                ]
                function = 6 if chunk.values == 1 and tag.width == 1 else 16
                await self._write_group_locked(
                    function, chunk.start, list(registers), self.write_retries
                )
        else:
            chunks = plan_chunks(
                address,
                1,
                len(values),
                max_registers=self.max_write_registers,
                max_bits=self.max_write_bits,
            )
            for chunk in chunks:
                bits = values[chunk.first : chunk.first + chunk.values]
                function = 5 if chunk.values == 1 else 15
                await self._write_group_locked(
                    function, chunk.start, list(bits), self.write_retries
                )

    async def _write_word_bit_locked(self, address: Address, value: bool) -> None:
        """Set one D word bit by reading the word and writing it back (RMW)."""
        bit = address.bit
        assert bit is not None
        group = ReadGroup(3, address.wire, 1, ())
        try:
            registers, _, _ = await self._read_group_locked(group, self.read_retries)
        except DeltaTimeoutError:
            # A write op owns the timeout rule: close so a late FC03 reply can
            # never be matched to a later request. Other errors leave the socket.
            self._client.close()
            raise
        word = set_bit(registers[0], bit, value)
        await self._write_group_locked(6, address.wire, [word], self.write_retries)

    async def _write_group_locked(
        self, function: int, start: int, values: list[Any], retries: int
    ) -> None:
        """Send one write PDU with up to *retries* extra attempts; or raise.

        A final timeout closes the socket first: a late response must never be
        matched to a later request. The next operation reconnects. Modbus
        exception responses are mapped and never retried.
        """
        attempt = 0
        while True:
            try:
                await self._write_once(function, start, values)
                return
            except (DeltaConnectionError, DeltaTimeoutError) as exc:
                if attempt >= retries:
                    if isinstance(exc, DeltaTimeoutError):
                        self._client.close()
                    raise
                if not self.is_connected:
                    try:
                        await self._connect_locked()
                    except DeltaPLCError:
                        pass
                await asyncio.sleep(_RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)])
                attempt += 1

    async def _write_once(self, function: int, start: int, values: list[Any]) -> None:
        """Issue one write PDU; a non-error response is success.

        Errors map exactly as ``_read_once``: timeouts to ``DeltaTimeoutError``,
        dropped connections to ``DeltaConnectionError``, a Modbus exception
        response to ``DeltaModbusException``, and anything else Modbus-shaped to
        ``DeltaProtocolError``.
        """
        try:
            method_name = _WRITE_METHOD_FOR[function]
        except KeyError:
            raise DeltaProtocolError(f"unsupported write function code {function}") from None
        argument: Any = values[0] if function in (5, 6) else values
        try:
            response = await getattr(self._client, method_name)(
                start, argument, device_id=self.unit
            )
        except ModbusIOException as exc:
            raise DeltaTimeoutError(f"{function}@{start}: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise DeltaTimeoutError(f"{function}@{start}: {exc}") from exc
        except ConnectionException as exc:
            raise DeltaConnectionError(f"{function}@{start}: {exc}") from exc
        except OSError as exc:
            raise DeltaConnectionError(f"{function}@{start}: {exc}") from exc
        except ModbusException as exc:
            raise DeltaProtocolError(f"{function}@{start}: {exc}") from exc

        if response.isError():
            function_code = getattr(response, "function_code", function) & 0x7F
            code = getattr(response, "exception_code", 0)
            raise DeltaModbusException(function_code, int(code))

    # -- read internals ----------------------------------------------------
    # The ``_read_*`` entry points acquire the lock themselves; the
    # ``*_locked`` helpers below run without it and require the caller to hold it.

    async def _read_typed(self, tag: str, dtype: DType | None, count: int) -> Any:
        """Shared body of ``read`` and the typed reads; validates before I/O."""
        parsed = make_tag(tag, dtype)
        if parsed.dtype is DType.BIT:
            if count != 1:
                raise DeltaValueError(f"{parsed.name} is a bit tag: count must be 1, got {count}")
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
                registers, _, _ = await self._read_group_locked(group, self.read_retries)
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
                registers, bits, elapsed_ms = await self._read_group_locked(group, retries)
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
                await asyncio.sleep(_RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)])
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
            raise DeltaProtocolError(f"unsupported read function code {group.function}") from None
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
                f"{group.function}@{group.start}: expected {group.count} values, got {received}"
            )
        return registers[: group.count], bits, elapsed_ms
