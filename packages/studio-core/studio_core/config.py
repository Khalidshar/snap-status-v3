"""Load non-secret policy configuration (config/*.toml) separately from business logic."""

import os
import re
import tomllib
from dataclasses import dataclass
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


# Runtime settings for one application: gateway, jobs, storage and network limits.


@dataclass(frozen=True)
class Config:
    app: str
    data_root: Path
    owners: dict[str, tuple[str, ...]]
    command_allowlist: tuple[str, ...]
    budgets: dict[str, int]
    lease_seconds: int
    rate_window: int
    rate_limit: int
    max_active_per_actor: int
    aging_seconds: int
    signature_skew_seconds: int
    retention_seconds: int
    proxy_socket: Path
    network_hosts: tuple[str, ...]
    network_timeout: int
    network_max_bytes: int
    max_running: int
    network_user_agent: str = "studio-core/0.2"

    @classmethod
    def load(cls, path: Path) -> "Config":
        d = tomllib.loads(path.read_text(encoding="utf-8"))
        app = d["app"]
        if app not in {"snap-bot-v2", "status-bot-v2"}:
            raise ValueError("Unknown app")
        if d.get("mode") != "foundation-test":
            raise ValueError("Live mode is not implemented")
        root = Path(d["data_root"])
        if not root.is_absolute():
            root = path.resolve().parent / root
        sock = Path(d["network"]["socket"])
        if not sock.is_absolute():
            sock = root / sock
        raw_owners = d["gateway"]["owners"]
        if (
            not isinstance(raw_owners, dict)
            or not raw_owners
            or any(
                not isinstance(v, list) or not v or any(not isinstance(a, str) or not a for a in v)
                for v in raw_owners.values()
            )
        ):
            raise ValueError("Bad owners")
        owners = {k: tuple(v) for k, v in raw_owners.items()}
        if any(k not in {"telegram", "whatsapp", "scheduler"} for k in owners):
            raise ValueError("Bad owners")
        hosts = tuple(d["network"]["hosts"])
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", h) for h in hosts):
            raise ValueError("Invalid network host")
        c = cls(
            app,
            root.resolve(),
            owners,
            tuple(d["gateway"]["commands"]),
            d["jobs"]["budgets"],
            d["jobs"]["lease_seconds"],
            d["gateway"]["rate_window"],
            d["gateway"]["rate_limit"],
            d["gateway"]["max_active_per_actor"],
            d["jobs"]["aging_seconds"],
            d["gateway"]["signature_skew_seconds"],
            d["storage"]["retention_seconds"],
            sock.resolve(),
            hosts,
            d["network"]["timeout_seconds"],
            d["network"]["max_bytes"],
            d["jobs"]["max_running"],
            d["network"].get("user_agent", "studio-core/0.2"),
        )
        if set(c.budgets) != {"CRITICAL", "HIGH", "NORMAL", "BACKGROUND"}:
            raise ValueError("Invalid priorities")
        numbers = [
            *c.budgets.values(),
            c.lease_seconds,
            c.rate_window,
            c.rate_limit,
            c.max_active_per_actor,
            c.aging_seconds,
            c.signature_skew_seconds,
            c.retention_seconds,
            c.network_timeout,
            c.network_max_bytes,
            c.max_running,
        ]
        if any(type(n) is not int or n <= 0 for n in numbers):
            raise ValueError("Positive integer required")
        if (
            not c.command_allowlist
            or any(not isinstance(x, str) or not x for x in c.command_allowlist)
            or c.network_max_bytes > 16 * 1024 * 1024
            or c.retention_seconds > 7 * 86400
            or c.max_running > 2
        ):
            raise ValueError("Invalid limits")
        if (
            not isinstance(c.network_user_agent, str)
            or not 1 <= len(c.network_user_agent) <= 256
            or any(ord(x) < 32 for x in c.network_user_agent)
        ):
            raise ValueError("Invalid User-Agent")
        return c
