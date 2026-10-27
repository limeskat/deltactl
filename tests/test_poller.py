# tests/test_poller.py
import asyncio
from datetime import datetime
import pytest
from delta_plc.poller import Frame, TagPoller
from delta_plc.planning import ReadGroup
from delta_plc.transport import GroupResult
from delta_plc.errors import DeltaConnectionError, PollerError

def result(group, value, error=None):
    return GroupResult(group, registers=[value], bits=[bool(value)],
                       error=error, elapsed_ms=1.25)

class FakeClient:
    def __init__(self, values=(1, 2), fail_fc=()):
        self.values = list(values)
        self.fail_fc = set(fail_fc)
        self.calls = 0
        self.connected = True
        self.connect_times = []
        self.now = 0.0
    @property
    def is_connected(self):
        return self.connected
    async def connect(self):
        self.connect_times.append(self.now)
        self.connected = True
    async def read_planned(self, groups):
        self.calls += 1
        idx = min(self.calls - 1, len(self.values) - 1)
        out = []
        for group in groups:
            err = DeltaConnectionError("down") if group.function in self.fail_fc else None
            out.append(result(group, self.values[idx], err))
        return out

class FakeClock:
    def __init__(self): self.t = 0.0
    def now(self): return self.t
    async def sleep(self, seconds): self.t += seconds

async def test_good_frame_schema():
    client, clock = FakeClient(), FakeClock()
    frames = []
    poller = TagPoller(client, ["D100", "M0"], interval=0.5, clock=clock.now, sleep=clock.sleep)
    await poller.run(count=1, writer=type("W", (), {"write": frames.append, "flush": lambda s: None, "close": lambda s: None})())
    assert len(frames) == 1
    frame = frames[0]
    assert frame.seq == 1 and frame.quality == "GOOD"
    assert frame.rtt_ms == 2.5 and frame.error is None
    assert list(frame.values) == ["D100", "M0"]
    assert datetime.fromisoformat(frame.ts).tzinfo is not None

async def test_degraded_and_bad():
    client = FakeClient(fail_fc={3})
    poller = TagPoller(client, ["D100", "M0"], interval=0.5)
    frames = []
    await poller.run(count=1, writer=type("W", (), {"write": frames.append, "flush": lambda s: None, "close": lambda s: None})())
    assert frames[0].quality == "DEGRADED"
    assert "D100" not in frames[0].values and "M0" in frames[0].values

async def test_disconnected_emits_bad_and_backs_off():
    client, clock = FakeClient(), FakeClock()
    client.connected = False
    async def failing_connect():
        client.connect_times.append(clock.t)
        raise DeltaConnectionError("no route")
    client.connect = failing_connect
    poller = TagPoller(client, ["D100"], interval=1.0, clock=clock.now,
                       sleep=clock.sleep, rand=lambda: 0.5)
    frames = []
    await poller.run(count=16, writer=type("W", (), {"write": frames.append, "flush": lambda s: None, "close": lambda s: None})())
    assert all(f.quality == "BAD" and f.values == {} for f in frames)
    assert client.connect_times[:5] == [0.0, 1.0, 3.0, 7.0, 15.0]

def test_interval_floor():
    with pytest.raises(PollerError):
        TagPoller(FakeClient(), ["D100"], interval=0.01)

async def test_duration_and_count_conflict():
    poller = TagPoller(FakeClient(), ["D100"], interval=0.5)
    with pytest.raises(PollerError):
        await poller.run(duration=1, count=2)

async def test_duration_and_count():
    client, clock = FakeClient(), FakeClock()
    poller = TagPoller(client, ["D100"], interval=1.0, clock=clock.now, sleep=clock.sleep)
    assert await poller.run(duration=0) == 0
    poller = TagPoller(client, ["D100"], interval=1.0, clock=clock.now, sleep=clock.sleep)
    assert await poller.run(count=3) == 3