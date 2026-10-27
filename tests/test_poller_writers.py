# tests/test_poller_writers.py
import asyncio
import csv
import io
import json
import math

from delta_plc.poller import CsvWriter, Frame, NdjsonWriter, TableWriter, TagPoller
from tests.test_poller import FakeClient, FakeClock, result


class Capture:
    def __init__(self, frames):
        self.frames = frames

    def write(self, frame):
        self.frames.append(frame)

    def flush(self):
        pass

    def close(self):
        pass


def frame(**kw):
    base = {
        "ts": "2026-10-25T21:22:05.104+05:30",
        "seq": 1,
        "rtt_ms": 4.2,
        "quality": "GOOD",
        "values": {"D100": 1, "M0": True},
    }
    base.update(kw)
    return Frame(**base)


def test_on_change_emits_first_and_changed():
    client, clock = FakeClient(values=(1, 1, 3)), FakeClock()
    poller = TagPoller(
        client,
        ["D100"],
        interval=1.0,
        on_change=True,
        deadband=2.0,
        clock=clock.now,
        sleep=clock.sleep,
    )
    frames = []

    async def go():
        for _ in range(3):
            await poller._poll_once(clock.now(), Capture(frames))
            clock.t += 1

    asyncio.run(go())
    assert [f.values for f in frames] == [{"D100": 1}, {"D100": 3}]


def test_seq_counts_only_emitted_frames():
    client, clock = FakeClient(values=(1, 1, 3)), FakeClock()
    poller = TagPoller(
        client,
        ["D100"],
        interval=1.0,
        on_change=True,
        deadband=2.0,
        clock=clock.now,
        sleep=clock.sleep,
    )
    frames = []

    async def go():
        for _ in range(3):
            await poller._poll_once(clock.now(), Capture(frames))
            clock.t += 1

    asyncio.run(go())
    assert [f.seq for f in frames] == [1, 2]


def test_heartbeat_full_snapshot():
    client, clock = FakeClient(values=(7, 7)), FakeClock()
    poller = TagPoller(
        client,
        ["D100", "M0"],
        interval=1.0,
        on_change=True,
        heartbeat=2.0,
        clock=clock.now,
        sleep=clock.sleep,
    )
    frames = []

    async def go():
        for _ in range(3):
            await poller._poll_once(clock.now(), Capture(frames))
            clock.t += 1

    asyncio.run(go())
    assert frames[0].values == {"D100": 7, "M0": True}
    assert frames[-1].heartbeat is True
    assert frames[-1].values == {"D100": 7, "M0": True}


def test_quality_change_emits_even_when_values_match():
    from delta_plc.errors import DeltaProtocolError

    class Q:
        connected = True
        is_connected = True
        calls = 0

        async def connect(self):
            return True

        async def read_planned(self, groups):
            Q.calls += 1
            err = None if Q.calls == 1 else DeltaProtocolError("bad")
            return [result(g, 1, err) for g in groups]

    poller = TagPoller(Q(), ["D100"], interval=1.0, on_change=True)
    frames = []

    async def go():
        await poller._poll_once(0.0, Capture(frames))
        await poller._poll_once(1.0, Capture(frames))

    asyncio.run(go())
    assert [f.quality for f in frames] == ["GOOD", "BAD"]


def test_per_tag_deadband_overrides_global():
    from dataclasses import replace

    from delta_plc.planning import make_tag

    client, clock = FakeClient(values=(1, 3, 5)), FakeClock()
    tag = replace(make_tag("D100"), deadband=3.0)
    poller = TagPoller(
        client,
        [tag],
        interval=1.0,
        on_change=True,
        deadband=1.0,
        clock=clock.now,
        sleep=clock.sleep,
    )
    frames = []

    async def go():
        for _ in range(3):
            await poller._poll_once(clock.now(), Capture(frames))
            clock.t += 1

    asyncio.run(go())
    assert [f.values["D100"] for f in frames] == [1, 5]


def test_ndjson_nonfinite_is_null():
    out = io.StringIO()
    NdjsonWriter(out).write(frame(values={"D100": math.nan, "D101": math.inf, "M0": True}))
    parsed = json.loads(out.getvalue())
    assert parsed["values"] == {"D100": None, "D101": None, "M0": True}


def test_csv_quotes_error_and_formats_values():
    out = io.StringIO()
    writer = CsvWriter(out, ["D100", "M0", "D102"])
    writer.write(
        frame(quality="DEGRADED", error='bad, "quoted"\nline', values={"D100": 1.5, "M0": False})
    )
    rows = list(csv.reader(io.StringIO(out.getvalue())))
    assert rows[0] == ["ts", "seq", "rtt_ms", "quality", "error", "D100", "M0", "D102"]
    assert rows[1][4] == 'bad, "quoted"\nline'
    assert rows[1][5:] == ["1.5", "false", ""]


def test_table_ascii_and_missing_values():
    out = io.StringIO()
    TableWriter(out, ["D100", "M0"]).write(frame(values={"D100": 3}))
    text = out.getvalue()
    assert "\x1b" not in text
    assert "D100" in text and "M0" in text and "-" in text
