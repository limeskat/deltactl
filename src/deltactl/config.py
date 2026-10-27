"""Persistent configuration management for deltactl."""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from pathlib import Path
from typing import Any

__all__ = [
    "clear_config",
    "get_config_path",
    "load_config",
    "resolve_setting",
    "save_config",
]


def get_config_path() -> Path:
    """Return the platform-specific path to the deltactl configuration file.

    On Windows:
        Returns ``%APPDATA%\\deltactl\\config.json`` (fallback ``~/AppData/Roaming/deltactl/config.json``).
    On POSIX:
        Returns ``$XDG_CONFIG_HOME/deltactl/config.json`` (fallback ``~/.config/deltactl/config.json``).
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "deltactl" / "config.json"
        return Path.home() / "AppData" / "Roaming" / "deltactl" / "config.json"

    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "deltactl" / "config.json"
    return Path.home() / ".config" / "deltactl" / "config.json"


def load_config() -> dict[str, Any]:
    """Load configuration from disk.

    Returns an empty dictionary if the configuration file does not exist,
    is corrupt, or has a schema_version greater than 1.
    """
    path = get_config_path()
    if not path.is_file():
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        warnings.warn(f"failed to read config file {path}: {exc}")
        return {}

    if not isinstance(data, dict):
        warnings.warn(f"config file {path} does not contain a JSON object")
        return {}

    schema_version = data.get("schema_version", 1)
    if isinstance(schema_version, (int, float)) and schema_version > 1:
        warnings.warn(f"unsupported config schema_version: {schema_version}")
        return {}

    return data


def save_config(updates: dict[str, Any]) -> None:
    """Save configuration updates atomically to disk, preserving existing unknown keys.

    Sets ``schema_version: 1`` in the saved file.
    """
    path = get_config_path()
    data = load_config()
    data.update(updates)
    data["schema_version"] = 1

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def clear_config() -> bool:
    """Remove the configuration file if it exists.

    Returns:
        True if the file was removed, False if it did not exist.
    """
    path = get_config_path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def resolve_setting(
    args: argparse.Namespace,
    attr: str,
    env_var: str,
    config_key: str,
    default: Any = None,
) -> Any:
    """Resolve a configuration setting following the precedence order:

    1. CLI flag on ``args``
    2. Environment variable ``env_var``
    3. Key in configuration file
    4. ``default``
    """
    val = getattr(args, attr, None)
    if val is not None:
        return val

    if env_var:
        env_val = os.environ.get(env_var)
        if env_val is not None and env_val != "":
            return env_val

    cfg_val = load_config().get(config_key)
    if cfg_val is not None:
        return cfg_val

    return default
