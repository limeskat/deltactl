# tests/test_cli_watch.py
import csv
import io
import json

import pytest

from deltactl.cli import main
from deltactl.mock import MockServer


@pytest.fixture(scope="module")
def mock():
    server = MockServer(port=0, seed={"D100": 42, "M0": True})
    server.start()
    yield server
    server.stop()


def run(mock, *argv):
    return main([*argv, "--host", "127.0.0.1", "--port", str(mock.address[1]), "--timeout", "1"])


def test_ndjson_default_and_json_alias(capsys, mock):
    assert run(mock, "watch", "D100", "M0", "--count", "2") == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    payload = json.loads(lines[0])
    assert payload["values"] == {"D100": 42, "M0": True}
    assert run(mock, "watch", "D100", "--count", "1", "--json") == 0
    assert json.loads(capsys.readouterr().out)["values"]["D100"] == 42


def test_csv_and_table(capsys, mock):
    assert run(mock, "watch", "D100", "M0", "--count", "1", "--format", "csv") == 0
    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    assert rows[0][:5] == ["ts", "seq", "rtt_ms", "quality", "error"]
    assert rows[0][5:] == ["D100", "M0"]
    assert run(mock, "watch", "D100", "--count", "1", "--format", "table") == 0
    out = capsys.readouterr().out
    assert "D100" in out and "\x1b" not in out


def test_watch_usage_errors(capsys, mock):
    with pytest.raises(SystemExit) as exc:
        main(["watch", "D100", "--duration", "1", "--count", "2", "--host", "127.0.0.1"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        main(["watch", "D100", "--format", "xml", "--host", "127.0.0.1"])
    with pytest.raises(SystemExit):
        main(["watch", "D100", "--dtype", "f32", "M0", "--host", "127.0.0.1"])


def test_watch_on_change(capsys, mock):
    assert run(mock, "watch", "D100", "--count", "1", "--on-change", "--deadband", "0") == 0
    assert json.loads(capsys.readouterr().out)["values"] == {"D100": 42}


def test_sigint_returns_130(monkeypatch, mock):
    async def boom(self, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("deltactl.poller.TagPoller.run", boom)
    assert run(mock, "watch", "D100") == 130
