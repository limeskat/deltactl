# tests/test_cli_series.py
import pytest

from deltactl.cli import main
from deltactl.mock import MockServer


@pytest.fixture
def mock_se():
    server = MockServer(port=0, seed={"D1001": 0x7C21})
    server.start()
    yield server
    server.stop()


def test_series_precedence(mock_se, tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("deltactl.config.get_config_path", lambda: cfg_file)
    cfg_file.write_text('{"schema_version": 1, "series": "SS2"}')

    # Config file series used if no flag/env
    assert (
        main(
            [
                "read",
                "D100",
                "--host",
                "127.0.0.1",
                "--port",
                str(mock_se.address[1]),
                "--timeout",
                "1",
            ]
        )
        == 0
    )

    # Env overrides config
    monkeypatch.setenv("DELTA_PLC_SERIES", "ES2")
    assert (
        main(
            [
                "read",
                "D100",
                "--host",
                "127.0.0.1",
                "--port",
                str(mock_se.address[1]),
                "--timeout",
                "1",
            ]
        )
        == 0
    )

    # Flag overrides env and config
    assert (
        main(
            [
                "read",
                "D100",
                "--series",
                "SE",
                "--host",
                "127.0.0.1",
                "--port",
                str(mock_se.address[1]),
                "--timeout",
                "1",
            ]
        )
        == 0
    )


def test_series_detect_command(mock_se, tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("deltactl.config.get_config_path", lambda: cfg_file)

    assert (
        main(["series-detect", "--host", "127.0.0.1", "--port", str(mock_se.address[1]), "--save"])
        == 0
    )
    out = capsys.readouterr().out
    assert "SE" in out and "0x7C21" in out
    # Verify saved
    assert "SE" in cfg_file.read_text()


def test_doctor_rejects_strict_series(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["doctor", "--strict-series"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--strict-series cannot be used with doctor; use --strict instead" in err


def test_unknown_series_flag(mock_se, capsys):
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "read",
                "D100",
                "--series",
                "NONEXISTENT_SERIES",
                "--host",
                "127.0.0.1",
                "--port",
                str(mock_se.address[1]),
            ]
        )
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unknown series 'NONEXISTENT_SERIES'" in err


def test_series_detect_no_save(mock_se, tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("deltactl.config.get_config_path", lambda: cfg_file)

    assert main(["series-detect", "--host", "127.0.0.1", "--port", str(mock_se.address[1])]) == 0
    out = capsys.readouterr().out
    assert "SE" in out and "0x7C21" in out
    assert not cfg_file.exists()


def test_series_detect_failure(capsys):
    # Port 1 is closed; connect should fail and return 1
    assert main(["series-detect", "--host", "127.0.0.1", "--port", "1", "--timeout", "0.2"]) == 1
    err = capsys.readouterr().err
    assert "error:" in err


def test_strict_series_abort_on_mismatch(mock_se, capsys):
    # Hardware is SE (0x7C21); configured is SS2 with --strict-series
    assert (
        main(
            [
                "read",
                "D100",
                "--series",
                "SS2",
                "--strict-series",
                "--host",
                "127.0.0.1",
                "--port",
                str(mock_se.address[1]),
            ]
        )
        == 1
    )


def test_write_dry_run_out_of_range_series(capsys):
    assert main(["write", "D8000", "1234", "--series", "SS2", "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "outside the D range" in err


def test_read_out_of_range_series_without_connecting(capsys):
    assert main(["read", "D8000", "--series", "SS2"]) == 2
    err = capsys.readouterr().err
    assert "outside the D range" in err
