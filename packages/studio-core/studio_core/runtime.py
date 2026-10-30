"""Shared command-line runtime used by each bot's entrypoint (healthcheck / dry-run)."""

import argparse
import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
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
    news_renderer: Callable[[Path, Path], dict[str, Any]] | None = None


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
    sources = commands.add_parser(
        "sources", help="Inspect command sources without network or delivery"
    )
    sources.add_argument(
        "--command", dest="source_command", help="Command key, Arabic name or number"
    )
    check = commands.add_parser(
        "source-check", help="Audit candidate XML through Network Core; no delivery"
    )
    check.add_argument("--command", dest="source_command", required=True)
    check.add_argument("--network-config", required=True, type=str)
    collect = commands.add_parser(
        "news-collect", help="Collect one live Arabic news review; no delivery"
    )
    collect.add_argument("--command", dest="source_command", required=True)
    collect.add_argument("--network-config", type=Path, required=True)
    collect.add_argument("--output", type=Path, required=True)
    render = commands.add_parser(
        "news-render", help="Render an offline silent news review; no delivery"
    )
    render.add_argument("--input", dest="news_input", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    return parser


def run_cli(spec: AppSpec, argv: Sequence[str] | None = None) -> int:
    args = _build_parser(spec).parse_args(argv)
    base = {"app": spec.name, "version": spec.version, "core_version": CORE_VERSION}
    if args.command == "sources":
        from studio_core.commands import COMMANDS
        from studio_core.sources import load, plan

        try:
            catalog = load()
            keys = (
                [args.source_command]
                if args.source_command is not None
                else [command.key for command in COMMANDS]
            )
            plans = [plan(key, catalog=catalog) for key in keys]
        except (OSError, ValueError):
            _emit({**base, "ok": False, "error": "تعذّر قراءة سجل المصادر أو تحديد الأمر."})
            return EXIT_CONFIG_ERROR
        _emit({**base, "ok": True, "plans": plans, "external_delivery": False})
        return EXIT_OK
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

    if args.command in {"news-collect", "news-render"}:
        from studio_core.sources.preview import prepare

        try:
            if args.command == "news-collect":
                result = prepare(spec.name, args.source_command, args.network_config, args.output)
            elif spec.news_renderer is not None:
                result = spec.news_renderer(args.news_input, args.output)
            else:
                raise ValueError("No app news renderer")
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
            _emit({**base, "ok": False, "error": "تعذّرت معاينة الخبر؛ لم يُرسل شيء."})
            return EXIT_CONFIG_ERROR
        _emit({**base, "ok": True, **result})
        return EXIT_OK

    if args.command == "source-check":
        from studio_core.config import Config
        from studio_core.network.client import NetworkClient
        from studio_core.sources.audit import audit

        try:
            network_config = Config.load(Path(args.network_config))
            if network_config.app != spec.name + "-v2":
                raise ValueError("Source audit app mismatch")
            checks = audit(args.source_command, NetworkClient(network_config))
        except (OSError, ValueError, KeyError):
            _emit(
                {
                    **base,
                    "ok": False,
                    "error": "تعذّر فحص المصادر؛ تحقق من الأمر وإعدادات شبكة التطبيق.",
                }
            )
            return EXIT_CONFIG_ERROR
        verified = any(check["result"] == "XML_VERIFIED" for check in checks)
        complete = verified and all(
            check["result"] in {"XML_VERIFIED", "EXCLUDED", "NOT_A_FEED"} for check in checks
        )
        _emit({**base, "ok": complete, "checks": checks, "external_delivery": False})
        return EXIT_OK if complete else EXIT_CONFIG_ERROR

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
