# tests/test_cli_doctor.py
import json

import pytest

from deltactl.cli import _parse_listen, main
from deltactl.mock import MockServer


@pytest.fixture(scope="module")
def mock():
    server = MockServer(port=0)
    server.start()
    yield server
    server.stop()


def test_listen_parser():
    assert _parse_listen("127.0.0.1:5020") == ("127.0.0.1", 5020)
    assert _parse_listen("[::1]:0") == ("::1", 0)
    with pytest.raises(ValueError):
        _parse_listen("nope")


def test_doctor_pass_json_and_strict(capsys, mock):
    assert main(["doctor", "--host", "127.0.0.1", "--port", str(mock.address[1]), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "PASS"
    assert main(["doctor", "--host", "127.0.0.1", "--port", "1", "--timeout", "0.2"]) == 1


def test_doctor_passive_and_strict_warn(capsys):
    assert main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"] is None


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "deltactl 0.2.0" in capsys.readouterr().out


def test_doctor_table_format(capsys, mock):
    assert main(["doctor", "--host", "127.0.0.1", "--port", str(mock.address[1])]) == 0
    out = capsys.readouterr().out
    assert "verdict: PASS" in out


def test_cli_logging_flags(mock):
    assert main(["-v", "read", "D100", "--host", "127.0.0.1", "--port", str(mock.address[1])]) == 0
    assert main(["-vv", "read", "D100", "--host", "127.0.0.1", "--port", str(mock.address[1])]) == 0
    assert main(["-q", "read", "D100", "--host", "127.0.0.1", "--port", str(mock.address[1])]) == 0


def test_cli_mock_command(monkeypatch, tmp_path):
    seed_file = tmp_path / "seed.json"
    seed_file.write_text(json.dumps({"D100": 42}))

    def fake_serve(self):
        raise KeyboardInterrupt

    monkeypatch.setattr("deltactl.mock.MockServer.serve", fake_serve)
    assert main(["mock", "--listen", "127.0.0.1:0", "--seed", str(seed_file)]) == 0

    bad_seed = tmp_path / "bad.json"
    bad_seed.write_text(json.dumps([1, 2, 3]))
    assert main(["mock", "--listen", "127.0.0.1:0", "--seed", str(bad_seed)]) == 2
