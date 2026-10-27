# tests/test_cli_doctor.py
import json
import pytest
from delta_plc.cli import main, _parse_listen
from delta_plc.mock import MockServer

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
    assert "0.1.0" in capsys.readouterr().out
