"""Load non-secret policy configuration (config/*.toml) separately from business logic."""

import os
import tomllib
from pathlib import Path
from typing import Any

CONFIG_ENV_VAR = "STUDIO_CONFIG"
DEFAULT_RELATIVE_PATH = Path("config/defaults.toml")


class ConfigError(RuntimeError):
    pass


def resolve_config_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    from_env = os.environ.get(CONFIG_ENV_VAR)
    if from_env:
        return Path(from_env)
    cwd = Path.cwd()
    for directory in (cwd, *cwd.parents):
        candidate = directory / DEFAULT_RELATIVE_PATH
        if candidate.is_file():
            return candidate
    raise ConfigError(f"No config found; set {CONFIG_ENV_VAR} or pass --config")


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    resolved = resolve_config_path(path)
    try:
        with resolved.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {resolved}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {resolved}: {exc}") from exc


def feature_enabled(config: dict[str, Any], name: str) -> bool:
    value = config.get("features", {}).get(name)
    if not isinstance(value, bool):
        raise ConfigError(f"Feature flag '{name}' is missing or not a boolean")
    return value
