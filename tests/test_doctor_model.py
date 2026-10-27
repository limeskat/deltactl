# tests/test_doctor_model.py
import json

import pytest

from deltactl.cli import main
from deltactl.mock import MockServer


@pytest.fixture
def mock_ss2():
    server = MockServer(port=0, seed={"D1001": 0x7210})
    server.start()
    yield server
    server.stop()


def test_doctor_reports_model_mismatch(mock_ss2, capsys):
    # Configured SE on SS2 hardware -> doctor warns
    assert (
        main(
            [
                "doctor",
                "--series",
                "SE",
                "--host",
                "127.0.0.1",
                "--port",
                str(mock_ss2.address[1]),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    model_checks = [c for c in payload["checks"] if c["name"] == "model"]
    assert len(model_checks) == 1
    assert model_checks[0]["status"] == "WARN"
    assert "SS2" in model_checks[0]["detail"]


def test_doctor_model_auto_pass(mock_ss2, capsys):
    assert (
        main(["doctor", "--host", "127.0.0.1", "--port", str(mock_ss2.address[1]), "--json"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    model_checks = [c for c in payload["checks"] if c["name"] == "model"]
    assert len(model_checks) == 1
    assert model_checks[0]["status"] == "PASS"
    assert "Detected model SS2" in model_checks[0]["detail"]


def test_doctor_model_match_pass(mock_ss2, capsys):
    assert (
        main(
            [
                "doctor",
                "--series",
                "SS2",
                "--host",
                "127.0.0.1",
                "--port",
                str(mock_ss2.address[1]),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    model_checks = [c for c in payload["checks"] if c["name"] == "model"]
    assert len(model_checks) == 1
    assert model_checks[0]["status"] == "PASS"
    assert "Model matches configured SS2" in model_checks[0]["detail"]


def test_doctor_model_unknown_code(capsys):
    server = MockServer(port=0, seed={"D1001": 0xFF00})
    server.start()
    try:
        assert (
            main(["doctor", "--host", "127.0.0.1", "--port", str(server.address[1]), "--json"]) == 0
        )
        payload = json.loads(capsys.readouterr().out)
        model_checks = [c for c in payload["checks"] if c["name"] == "model"]
        assert len(model_checks) == 1
        assert model_checks[0]["status"] == "WARN"
        assert "Unrecognized model code" in model_checks[0]["detail"]
    finally:
        server.stop()


def test_doctor_model_skip_on_tcp_fail(capsys):
    assert main(["doctor", "--host", "127.0.0.1", "--port", "1", "--timeout", "0.2", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    model_checks = [c for c in payload["checks"] if c["name"] == "model"]
    assert len(model_checks) == 1
    assert model_checks[0]["status"] == "SKIP"
    assert "Model check skipped because TCP 502 connection failed" in model_checks[0]["detail"]


def test_doctor_model_skip_on_modbus_fail(mock_ss2, monkeypatch, capsys):
    async def fake_read_many(self, tags):
        raise ConnectionResetError("connection reset by peer")

    monkeypatch.setattr("deltactl.doctor.AsyncDeltaPLC.read_many", fake_read_many)
    assert (
        main(["doctor", "--host", "127.0.0.1", "--port", str(mock_ss2.address[1]), "--json"]) == 1
    )
    payload = json.loads(capsys.readouterr().out)
    model_checks = [c for c in payload["checks"] if c["name"] == "model"]
    assert len(model_checks) == 1
    assert model_checks[0]["status"] == "SKIP"
    assert "Model check skipped due to Modbus failure" in model_checks[0]["detail"]
