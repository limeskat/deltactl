# tests/test_cli_read_write.py
import json

import pytest

from deltactl.cli import main
from deltactl.mock import MockServer


@pytest.fixture(scope="module")
def mock():
    server = MockServer(port=0, seed={"D100": 42, "M0": True, "X0": False})
    server.start()
    yield server
    server.stop()


def run(capsys, mock, *argv):
    return main([*argv, "--host", "127.0.0.1", "--port", str(mock.address[1]), "--timeout", "1"])


def test_read_human_and_json(capsys, mock):
    assert run(capsys, mock, "read", "D100", "M0") == 0
    out = capsys.readouterr().out
    assert "D100 = 42" in out and "M0 = true" in out
    assert run(capsys, mock, "read", "D100", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["values"] == {"D100": 42}


def test_negative_write_and_dtype(capsys, mock):
    assert run(capsys, mock, "write", "D100", "-1") == 0
    assert run(capsys, mock, "read", "D100") == 0
    assert "D100 = 65535" in capsys.readouterr().out
    assert run(capsys, mock, "write", "D100", "-0x10") == 0
    assert run(capsys, mock, "read", "D100") == 0
    assert "D100 = 65520" in capsys.readouterr().out
    assert run(capsys, mock, "write", "--dtype", "f32", "D200", "-12.5") == 0
    assert run(capsys, mock, "read", "--dtype", "f32", "D200") == 0
    assert "D200 = -12.5" in capsys.readouterr().out


def test_count_raw_dry_run(capsys, mock):
    assert run(capsys, mock, "write", "D100", "42") == 0
    assert run(capsys, mock, "read", "D100", "--count", "3") == 0
    assert "D100[0] = 42" in capsys.readouterr().out
    assert run(capsys, mock, "read", "--raw", "0x1064", "--raw-type", "d") == 0
    assert "D100 = 42" in capsys.readouterr().out
    assert run(capsys, mock, "write", "--raw", "0x1064", "7", "--dry-run") == 0
    assert "wire=0x1064" in capsys.readouterr().out


def test_guard_and_usage_errors(capsys, mock):
    assert run(capsys, mock, "write", "X0", "true") == 1
    assert "read-only" in capsys.readouterr().err.lower()
    assert (
        main(["read", "D4095", "--dtype", "f32", "--host", "203.0.113.1", "--timeout", "0.01"]) == 2
    )
    assert "error:" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main(["read", "D100", "M0", "--count", "2", "--host", "127.0.0.1"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        main(["read", "--raw", "0x1000", "D100"])
    with pytest.raises(SystemExit):
        main(["read", "D100", "M0", "--dtype", "f32"])


def test_dry_run_no_io(capsys):
    assert (
        main(["write", "D100", "1", "--dry-run", "--host", "203.0.113.1", "--timeout", "0.01"]) == 0
    )
    out = capsys.readouterr().out
    assert "FC" in out or "wire" in out.lower()


def test_range_expansion(capsys, mock):
    assert run(capsys, mock, "read", "D100-D102") == 0
    out = capsys.readouterr().out
    assert out.count(" = ") == 3


def test_env_precedence(capsys, mock, monkeypatch):
    monkeypatch.setenv("DELTA_PLC_HOST", "203.0.113.1")
    assert run(capsys, mock, "read", "D100") == 0


def test_multivalue_32bit_split_rejected_before_io(capsys):
    assert (
        main(
            [
                "write",
                "--dtype",
                "f32",
                "D4093",
                "1.0",
                "2.0",
                "--host",
                "203.0.113.1",
                "--timeout",
                "0.01",
            ]
        )
        == 2
    )
    assert "error:" in capsys.readouterr().err


def test_cli_read_write_additional_branches(capsys, mock, monkeypatch):
    assert run(capsys, mock, "read", "D100.0") == 0
    assert "D100.0 = " in capsys.readouterr().out
    assert run(capsys, mock, "read", "M0", "--count", "3") == 0
    assert "M0[0] = " in capsys.readouterr().out

    assert run(capsys, mock, "write", "D100.2", "true") == 0
    assert run(capsys, mock, "read", "D100.2") == 0
    assert "D100.2 = true" in capsys.readouterr().out

    assert run(capsys, mock, "write", "D200", "10", "20", "30") == 0
    assert run(capsys, mock, "read", "D200", "--count", "3") == 0
    out = capsys.readouterr().out
    assert "D200[0] = 10" in out and "D200[2] = 30" in out

    with pytest.raises(SystemExit):
        main(["read", "D100", "--count", "0"])

    with pytest.raises(SystemExit):
        main(["read", "D100", "--raw-type", "d"])
    with pytest.raises(SystemExit):
        main(["write", "D100", "1", "--raw-type", "d"])

    with pytest.raises(SystemExit):
        main(["read", "--raw", "0x0800", "--raw-type", "m", "--dtype", "f32"])
    with pytest.raises(SystemExit):
        main(["write", "--raw", "0x0800", "--raw-type", "m", "--dtype", "f32", "1"])

    with pytest.raises(SystemExit):
        main(["write", "D100"])

    assert run(capsys, mock, "write", "D100.1", "true", "false") == 2
    assert "exactly one value is required" in capsys.readouterr().err

    monkeypatch.setenv("DELTA_PLC_PORT", "not_a_num")
    assert main(["read", "D100", "--host", "127.0.0.1"]) == 2
    monkeypatch.delenv("DELTA_PLC_PORT")

    monkeypatch.setenv("DELTA_PLC_TIMEOUT", "not_a_float")
    assert main(["read", "D100", "--host", "127.0.0.1", "--port", str(mock.address[1])]) == 2
