"""Polling loop: frames, quality classification, and reconnect backoff.

:class:`TagPoller` turns a set of tags into a fixed read plan and runs it once
per interval. Every cycle emits at most one :class:`Frame`: ``GOOD`` when every
read group succeeded, ``DEGRADED`` when some failed (the frame carries only the
values that arrived), and ``BAD`` when all failed or the client is
disconnected.

While disconnected the poller emits a ``BAD`` frame per cycle and retries
``connect()`` on an exponential backoff (1, 2, 4, 8, 15 s, capped and jittered
by ``0.8 + 0.4 * rand()``). The clock, sleep, and random source are injectable
so tests run deterministically, and ``_poll_once`` is the single-cycle seam
used both by :meth:`TagPoller.run` and by filter tests.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .errors import DeltaPLCError, PollerError
from .planning import ReadGroup, Tag, make_tag, plan_reads
from .transport import GroupResult, decode_group_values

__all__ = ["Frame", "FrameWriter", "TagPoller"]

#: Reconnect delays in seconds; the last value repeats. Each one is scaled by
#: ``0.8 + 0.4 * rand()`` so a fleet of pollers does not reconnect in lockstep.
_BACKOFF_STEPS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 15.0)

#: Smallest poll interval the poller accepts, in seconds.
_MIN_INTERVAL: float = 0.05


@dataclass(frozen=True)
class Frame:
    """One poll result: when, which cycle, how fast, how good, and what values.

    ``error`` is set whenever the cycle did not fully succeed, and ``heartbeat``
    marks a full snapshot emitted only to prove liveness. ``values`` holds just
    the tags that decoded, in the poller's requested tag order.
    """

    ts: str
    seq: int
    rtt_ms: float
    quality: str
    values: dict[str, int | float | bool]
    error: str | None = None
    heartbeat: bool = False

    def to_dict(self) -> dict[str, object]:
        """Return the wire form: ``error`` only when set, ``heartbeat`` only when true."""
        payload: dict[str, object] = {
            "ts": self.ts,
            "seq": self.seq,
            "rtt_ms": self.rtt_ms,
            "quality": self.quality,
            "values": dict(self.values),
        }
        if self.error is not None:
            payload["error"] = self.error
        if self.heartbeat:
            payload["heartbeat"] = True
        return payload


class FrameWriter(Protocol):
    """Sink for emitted frames; ``flush``/``close`` are owned by the caller."""

    def write(self, frame: Frame) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


class _PollClient(Protocol):
    """The slice of the async client the poller depends on."""

    @property
    def is_connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def read_planned(self, groups: Sequence[ReadGroup]) -> list[GroupResult]: ...


def _error_text(exc: BaseException | None) -> str:
    """Human-readable *exc* text, falling back to its class name."""
    if exc is None:
        return "unknown error"
    return str(exc) or type(exc).__name__


class TagPoller:
    """Poll a fixed set of tags into frames, with quality and reconnect policy.

    Tags are parsed and planned once in ``__init__``, so every cycle reuses the
    same groups. ``on_change``, ``deadband``, and ``heartbeat`` are stored for
    the filter layer; this core emits one frame per cycle.
    """

    def __init__(
        self,
        client: _PollClient,
        tags: Sequence[str | Tag],
        *,
        interval: float = 0.5,
        on_change: bool = False,
        deadband: float = 0.0,
        heartbeat: float = 10.0,
        max_gap: int = 10,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        if interval < _MIN_INTERVAL:
            raise PollerError(
                f"interval must be at least {_MIN_INTERVAL}s, got {interval}"
            )
        if not tags:
            raise PollerError("TagPoller requires at least one tag")
        self._client = client
        self._tags: tuple[Tag, ...] = tuple(
            item if isinstance(item, Tag) else make_tag(item) for item in tags
        )
        self._max_gap = max_gap
        # Planned once: a fixed tag set always maps to the same read groups.
        self._groups: list[ReadGroup] = plan_reads(self._tags, max_gap=max_gap)
        self._interval = interval
        self._on_change = on_change
        self._deadband = deadband
        self._heartbeat = heartbeat
        self._clock = clock
        self._sleep = sleep
        self._rand = rand
        self._seq = 0
        self._stopped = False
        # Reconnect state: ``-inf`` means the first disconnected cycle connects.
        self._backoff_step = 0
        self._next_attempt_at = float("-inf")
        self._last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------

    async def run(
        self,
        *,
        duration: float | None = None,
        count: int | None = None,
        writer: FrameWriter | None = None,
    ) -> int:
        """Poll until *duration* elapses or *count* frames are emitted.

        At most one of *duration*/*count* may be set. The elapsed check runs
        before the first cycle, so ``duration=0`` emits nothing. Returns the
        number of frames emitted. Writer lifecycle (``flush``/``close``) belongs
        to the caller.
        """
        if duration is not None and count is not None:
            raise PollerError("run accepts duration or count, not both")
        start = self._clock()
        emitted = 0
        while not self._stopped:
            if duration is not None and self._clock() - start >= duration:
                break
            if count is not None and emitted >= count:
                break
            cycle_start = self._clock()
            emitted += await self._poll_once(cycle_start, writer)
            elapsed = self._clock() - cycle_start
            await self._sleep(max(0.0, self._interval - elapsed))
        return emitted

    def stop(self) -> None:
        """Ask the loop to exit after the current cycle; idempotent."""
        self._stopped = True

    # -- one cycle ---------------------------------------------------------

    async def _poll_once(self, now: float, writer: FrameWriter | None) -> int:
        """Run one poll cycle and emit at most one frame; return frames emitted.

        *now* drives reconnect scheduling so tests can advance a fake clock.
        """
        if self._client.is_connected:
            quality, values, rtt_ms, error = await self._read_cycle()
        else:
            error = await self._reconnect(now)
            quality, values, rtt_ms = "BAD", {}, 0.0
        self._seq += 1
        frame = Frame(
            ts=datetime.now().astimezone().isoformat(timespec="milliseconds"),
            seq=self._seq,
            rtt_ms=rtt_ms,
            quality=quality,
            values=values,
            error=error,
        )
        if writer is not None:
            writer.write(frame)
        return 1

    async def _read_cycle(
        self,
    ) -> tuple[str, dict[str, int | float | bool], float, str | None]:
        """Read the fixed plan and classify the cycle's quality."""
        try:
            results = await self._client.read_planned(self._groups)
        except (DeltaPLCError, OSError) as exc:
            # Defensive: group failures are reported in ``results``, but the
            # socket can drop between the is_connected check and the call.
            return "BAD", {}, 0.0, _error_text(exc)
        failed = [result for result in results if result.error is not None]
        if not failed:
            quality = "GOOD"
        elif len(failed) == len(results):
            quality = "BAD"
        else:
            quality = "DEGRADED"
        decoded: dict[str, int | float | bool] = {}
        for result in results:
            if result.error is None:
                decoded.update(
                    decode_group_values(result.group, result.registers, result.bits)
                )
        # Re-order into the requested tag order; groups are ordered by function.
        values = {tag.name: decoded[tag.name] for tag in self._tags if tag.name in decoded}
        # A dropped connection stops later groups too, so sum what we got.
        rtt_ms = round(sum(result.elapsed_ms for result in results), 2)
        error = _error_text(failed[0].error) if failed else None
        return quality, values, rtt_ms, error

    async def _reconnect(self, now: float) -> str:
        """Connect when the backoff is due and return this cycle's error text.

        A failure schedules the next attempt from the capped step ladder,
        jittered by ``0.8 + 0.4 * rand()``; a success resets the ladder and
        makes a later reconnect immediate.
        """
        if now < self._next_attempt_at:
            return self._last_error or "not connected"
        try:
            await self._client.connect()
        except (DeltaPLCError, OSError) as exc:
            step = min(self._backoff_step, len(_BACKOFF_STEPS) - 1)
            delay = _BACKOFF_STEPS[step] * (0.8 + 0.4 * self._rand())
            self._backoff_step += 1
            self._next_attempt_at = now + delay
            self._last_error = _error_text(exc)
            return self._last_error
        self._backoff_step = 0
        self._next_attempt_at = now
        self._last_error = None
        return "not connected"