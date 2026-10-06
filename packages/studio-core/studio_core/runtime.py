"""Shared command-line runtime used by each bot's entrypoint (healthcheck / dry-run)."""

import argparse
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from studio_core import __version__ as CORE_VERSION
from studio_core.config import ConfigError, feature_enabled, load_config
from studio_core.jobs import Job

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_DISABLED = 3


@dataclass(frozen=True)
class AppSpec:
    name: str
    version: str
    feature_flag: str
    default_content_type: str
    create_job: Callable[[str, str], Job]


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _build_parser(spec: AppSpec) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=spec.name, description=f"{spec.name} runtime")
    parser.add_argument(
        "--config", help="TOML config path (default: $STUDIO_CONFIG or config/defaults.toml)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("healthcheck", help="Validate config and report version/kill switch")
    dry_run = commands.add_parser("dry-run", help="Create a job without any delivery")
    dry_run.add_argument("--requested-by", default="ci")
    dry_run.add_argument("--content-type", default=spec.default_content_type)
    return parser


def run_cli(spec: AppSpec, argv: Sequence[str] | None = None) -> int:
    args = _build_parser(spec).parse_args(argv)
    base = {"app": spec.name, "version": spec.version, "core_version": CORE_VERSION}
    try:
        config = load_config(args.config)
        enabled = feature_enabled(config, spec.feature_flag)
    except ConfigError as exc:
        _emit({**base, "ok": False, "error": str(exc)})
        return EXIT_CONFIG_ERROR

    if args.command == "healthcheck":
        _emit({**base, "ok": True, "enabled": enabled})
        return EXIT_OK

    if not enabled:
        _emit({**base, "ok": False, "error": f"{spec.feature_flag} kill switch is off"})
        return EXIT_DISABLED

    job = spec.create_job(args.requested_by, args.content_type)
    _emit(
        {
            **base,
            "ok": True,
            "job": {
                "job_id": job.job_id,
                "project": job.project,
                "content_type": job.content_type,
                "destination": job.destination,
                "priority": job.priority.value,
                "status": job.status.value,
            },
        }
    )
    return EXIT_OK
