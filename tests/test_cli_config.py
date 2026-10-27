import pytest

from deltactl.cli import main


def test_cli_config_commands(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("deltactl.config.get_config_path", lambda: cfg_file)

    # set-series validates and saves canonical
    assert main(["config", "set-series", "DVP-14SS2"]) == 0
    assert "SS2" in capsys.readouterr().out

    # get-series returns saved
    assert main(["config", "get-series"]) == 0
    assert "SS2" in capsys.readouterr().out

    # show displays json
    assert main(["config", "show"]) == 0
    out = capsys.readouterr().out
    assert "config.json" in out and "SS2" in out

    # clear unlinks
    assert main(["config", "clear"]) == 0
    assert not cfg_file.exists()


def test_cli_config_get_series_unset(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("deltactl.config.get_config_path", lambda: cfg_file)

    assert main(["config", "get-series"]) == 1
    assert "No series configured" in capsys.readouterr().out


def test_cli_config_set_series_invalid(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("deltactl.config.get_config_path", lambda: cfg_file)

    with pytest.raises(SystemExit) as exc:
        main(["config", "set-series", "INVALID_SERIES"])
    assert exc.value.code == 2
