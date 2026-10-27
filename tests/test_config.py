import argparse
import json
from pathlib import Path

import pytest

from deltactl.config import (
    clear_config,
    get_config_path,
    load_config,
    resolve_setting,
    save_config,
)


def test_atomic_save_and_preserve_keys(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("deltactl.config.get_config_path", lambda: cfg_file)

    # Initial save
    save_config({"series": "SS2", "custom_key": "custom_val"})
    assert cfg_file.exists()
    data = json.loads(cfg_file.read_text())
    assert data["series"] == "SS2"
    assert data["custom_key"] == "custom_val"
    assert data["schema_version"] == 1

    # Update series preserves custom_key
    save_config({"series": "ES2"})
    data2 = load_config()
    assert data2["series"] == "ES2"
    assert data2["custom_key"] == "custom_val"

    clear_config()
    assert not cfg_file.exists()
    assert load_config() == {}
    assert clear_config() is False


def test_save_config_error_cleanup(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("deltactl.config.get_config_path", lambda: cfg_file)

    def _failing_replace(src, dst):
        raise OSError("disk failure")

    monkeypatch.setattr("os.replace", _failing_replace)
    with pytest.raises(OSError, match="disk failure"):
        save_config({"series": "SS2"})

    # Temp file should be cleaned up
    tmp_files = list(tmp_path.glob("config.json.tmp.*"))
    assert len(tmp_files) == 0


def test_schema_version_warning(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("deltactl.config.get_config_path", lambda: cfg_file)

    cfg_file.write_text(json.dumps({"schema_version": 2, "series": "SS2"}))
    with pytest.warns(UserWarning, match="unsupported config schema_version"):
        assert load_config() == {}


def test_invalid_json_handling(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("deltactl.config.get_config_path", lambda: cfg_file)

    cfg_file.write_text("not json {")
    with pytest.warns(UserWarning, match="failed to read config file"):
        assert load_config() == {}

    cfg_file.write_text(json.dumps(["not", "a", "dict"]))
    with pytest.warns(UserWarning, match="does not contain a JSON object"):
        assert load_config() == {}


def test_get_config_path_posix(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/config")
    assert get_config_path() == Path("/custom/config/deltactl/config.json")

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: Path("/home/testuser"))
    assert get_config_path() == Path("/home/testuser/.config/deltactl/config.json")


def test_get_config_path_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("APPDATA", "/win/appdata")
    assert get_config_path() == Path("/win/appdata/deltactl/config.json")

    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: Path("/win/home"))
    assert get_config_path() == Path("/win/home/AppData/Roaming/deltactl/config.json")


def test_resolve_setting(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("deltactl.config.get_config_path", lambda: cfg_file)
    cfg_file.write_text(json.dumps({"schema_version": 1, "series": "SS2"}))

    # Priority 1: CLI flag
    args = argparse.Namespace(series="SE")
    monkeypatch.setenv("DELTA_PLC_SERIES", "ES2")
    assert resolve_setting(args, "series", "DELTA_PLC_SERIES", "series", "DEFAULT") == "SE"

    # Priority 2: Env var
    args_empty = argparse.Namespace(series=None)
    assert resolve_setting(args_empty, "series", "DELTA_PLC_SERIES", "series", "DEFAULT") == "ES2"

    # Priority 3: Config file
    monkeypatch.delenv("DELTA_PLC_SERIES", raising=False)
    assert resolve_setting(args_empty, "series", "DELTA_PLC_SERIES", "series", "DEFAULT") == "SS2"

    # Empty env var should fall through to config
    monkeypatch.setenv("DELTA_PLC_SERIES", "")
    assert resolve_setting(args_empty, "series", "DELTA_PLC_SERIES", "series", "DEFAULT") == "SS2"

    # Empty env_var name should fall through to config
    assert resolve_setting(args_empty, "series", "", "series", "DEFAULT") == "SS2"

    # Priority 4: Default
    clear_config()
    assert (
        resolve_setting(args_empty, "series", "DELTA_PLC_SERIES", "series", "DEFAULT") == "DEFAULT"
    )
