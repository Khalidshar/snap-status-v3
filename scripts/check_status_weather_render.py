"""Disposable real no-Internet systemd FFmpeg render; synthetic weather, no sending.

Ported from the lab (status-bot-v2 tools/check_weather_render.py). --bundle keeps the
owned job state (VALIDATING lease) for the separate HUMAN_FRAME test send.
Usage (disposable Linux root with systemd as PID 1, e.g. a CI runner):
  sudo -n "$(uv run --frozen python -c 'import sys;print(sys.executable)')" \
      scripts/check_status_weather_render.py
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/status-bot"))
sys.path.insert(0, str(ROOT / "apps/status-bot/tests"))

from test_status_weather import FixtureClient  # noqa: E402

import studio_core  # noqa: E402  # after the app paths above
from status_bot.pipeline import prepare, record_validation  # noqa: E402
from status_bot.whatsapp_contract import DeliveryBlocked, DeliveryIdentity  # noqa: E402
from status_bot.whatsapp_outbox import WhatsAppOutbox  # noqa: E402
from studio_core.config import Config  # noqa: E402
from studio_core.jobs import Repository  # noqa: E402
from studio_core.storage import Storage  # noqa: E402

CORE = str(Path(studio_core.__file__).resolve().parents[1])

BUNDLE_SECONDS = 3600
REPOSITORY = "Khalidshar/status-bot-v2"
RUNNER = """import json,sys
from pathlib import Path
from dataclasses import replace
p=json.loads(Path(sys.argv[1]).read_text())
sys.path[:0]=[p['app_root'],p['core_root']]
from studio_core.config import Config
from status_bot.render import render
cfg=replace(Config.load(Path(p['config'])),data_root=Path(p['data_root']))
print(json.dumps(render(cfg,p['settings'],p['job'],p['digest'])))
"""


def run(args: list[Any], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    # Fixed argv built here from pinned tools; never a shell.
    result = subprocess.run(  # noqa: S603  # fixed argv, no shell
        list(map(str, args)), capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(
            "Synthetic render command failed: " + result.stdout[-4000:] + result.stderr[-4000:]
        )
    return result


def write_bundle(
    output: Path, cfg: Config, prepared: dict[str, Any], result: Any, run_id: str, head_sha: str
) -> None:
    if output.exists():
        raise ValueError("Bundle output must be new")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o750)
    job = prepared["claim"]["id"]
    workspace = Storage(cfg).workspace(job)
    shutil.copytree(
        cfg.data_root, output / "state", ignore=shutil.ignore_patterns("jobs.sqlite*", "*.sock")
    )
    with (
        sqlite3.connect(cfg.data_root / "jobs.sqlite") as source,
        sqlite3.connect(output / "state/jobs.sqlite") as target,
    ):
        source.backup(target)
    for name in ("weather.mp4", "preview.png"):
        shutil.copyfile(workspace / "output" / name, output / name)
    metadata = {
        "schema": 1,
        "repository": REPOSITORY,
        "run_id": run_id,
        "head_sha": head_sha,
        "job_id": job,
        "lease_token": prepared["claim"]["lease_token"],
        "render": result,
        "content_hash": result["content_hash"],
        "video_sha256": result["video_sha256"],
        "preview_sha256": hashlib.sha256((output / "preview.png").read_bytes()).hexdigest(),
        "expires_at": prepared["content"]["expires_at"],
        "synthetic_source": prepared["synthetic_source"],
        "delivery": "NOT_ATTEMPTED",
        "human_approval": None,
    }
    (output / "preview.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    uid = int(os.environ.get("SUDO_UID", os.getuid()))
    gid = int(os.environ.get("SUDO_GID", os.getgid()))
    for path in [output, *output.rglob("*")]:
        os.chown(path, uid, gid)
        path.chmod(0o750 if path.is_dir() else 0o640)
    print(
        json.dumps(
            {
                "content_hash": result["content_hash"],
                "video_sha256": result["video_sha256"],
                "job_id": job,
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--head-sha", default="")
    options = parser.parse_args()
    if options.bundle and (
        not re.fullmatch("[1-9][0-9]{0,19}", options.run_id)
        or not re.fullmatch("[0-9a-f]{40}", options.head_sha)
    ):
        raise SystemExit("Bundle provenance required")
    if os.geteuid() != 0 or Path("/proc/1/comm").read_text().strip() != "systemd":
        raise SystemExit("Disposable Linux root/systemd required")
    root = Path(tempfile.mkdtemp(prefix="madar-status-weather-", dir="/var/lib"))
    unit = "madar-status-fixture-" + secrets.token_hex(4) + ".service"
    listener = None
    started = False
    try:
        fixture_apps = root / "apps"
        fixture_apps.mkdir()
        shutil.copytree(ROOT / "apps/status-bot/status_bot", fixture_apps / "status_bot")
        base = root / "weather.toml"
        shutil.copyfile(ROOT / "config/status/weather.toml", base)
        cfg = replace(Config.load(base), data_root=root / "data")
        if options.bundle:
            # Review window: the owner must inspect the preview before the separate send job.
            cfg = replace(
                cfg, lease_seconds=BUNDLE_SECONDS, budgets={**cfg.budgets, "NORMAL": BUNDLE_SECONDS}
            )
        settings = tomllib.loads(base.read_text())["slice"]
        profile = settings["profile"]
        for name, tool in [("ffmpeg", "ffmpeg"), ("ffprobe", "ffprobe"), ("fc_query", "fc-query")]:
            profile[name] = shutil.which(tool)
            if not profile[name]:
                raise RuntimeError("Missing tool " + tool)
        for name, path in [
            ("font", "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"),
            ("latin_font", "/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
        ]:
            profile[name] = path
            profile[name + "_sha256"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        profile["ffmpeg_version"] = run([profile["ffmpeg"], "-version"]).stdout.splitlines()[0]
        settings["egress_socket"] = str(root / "egress.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(settings["egress_socket"])
        listener.listen(1)
        prepared = prepare(
            cfg,
            settings,
            FixtureClient(),
            "status-render-fixture-only-gateway-key",
            "fixture_weather",
            synthetic=True,
        )
        job = prepared["claim"]["id"]
        payload = root / "request.json"
        payload.write_text(
            json.dumps(
                {
                    "config": str(base),
                    "data_root": str(cfg.data_root),
                    "app_root": str(fixture_apps),
                    "core_root": CORE,
                    "settings": settings,
                    "job": job,
                    "digest": prepared["content"]["content_hash"],
                }
            )
        )
        runner = root / "runner.py"
        runner.write_text(RUNNER)
        args = [
            "systemd-run",
            "--quiet",
            "--wait",
            "--pipe",
            "--unit",
            unit,
            "--property=Type=oneshot",
            "--property=RestrictAddressFamilies=AF_UNIX",
            "--property=NoNewPrivileges=yes",
            "--property=CapabilityBoundingSet=",
            "--property=KillMode=control-group",
            "--property=TimeoutStartSec=300s",
            "--property=TimeoutStopSec=5s",
            "--property=InaccessiblePaths=" + settings["egress_socket"],
            "--setenv=PYTHONPATH=" + str(fixture_apps),
            "--setenv=LD_LIBRARY_PATH=" + str(Path(sys.base_prefix) / "lib"),
            sys.executable,
            str(runner),
            str(payload),
        ]
        started = True
        result = run(args, timeout=320)
        render_result = next(
            json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"job_id"')
        )
        manifest = record_validation(cfg, prepared, render_result)
        # A real CI video is still synthetic content and must never enter the
        # WhatsApp delivery path. This check has no transport or credentials.
        fixture_identity = DeliveryIdentity(
            "12345001",
            "12345002",
            "10000000000",
            b"unit-fixture-review-key-32-bytes-only",
            b"unit-fixture-webhook-key-32-bytes-only",
        )
        delivery_gate = WhatsAppOutbox(Repository(cfg), fixture_identity)
        try:
            delivery_gate.bundle(job, prepared["claim"]["attempt"])
        except DeliveryBlocked as rejection:
            if str(rejection) != "WA_MANIFEST_INVALID":
                raise
        else:
            raise RuntimeError("Synthetic CI render incorrectly eligible for WhatsApp")
        target = ROOT / "outputs/status-weather"
        target.mkdir(parents=True, exist_ok=True)
        workspace = Storage(cfg).workspace(job)
        for name in ("weather.mp4", "preview.png", "render-result.json"):
            shutil.copyfile(workspace / "output" / name, target / name)
        (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        if options.bundle:
            write_bundle(
                options.bundle.resolve(),
                cfg,
                prepared,
                render_result,
                options.run_id,
                options.head_sha,
            )
        print(
            json.dumps(
                {
                    "passed": True,
                    "qa": render_result["qa"],
                    "network_isolation": render_result["network_isolation"],
                    "synthetic_source": True,
                    "external_delivery": False,
                    "whatsapp_synthetic_delivery_blocked": True,
                    "artifact": "status-weather-video",
                    "visual_review": "PENDING",
                }
            )
        )
    finally:
        if started:
            for action in ("stop", "reset-failed"):
                subprocess.run(  # noqa: S603  # fixed argv, no shell
                    ["systemctl", action, unit],  # noqa: S607  # pinned system tool
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
        if listener:
            listener.close()
        resolved = root.resolve()
        if resolved.parent != Path("/var/lib") or not resolved.name.startswith(
            "madar-status-weather-"
        ):
            raise RuntimeError("Unsafe fixture cleanup")
        shutil.rmtree(resolved)
        print("STATUS_WEATHER_FIXTURE_CLEANUP_VERIFIED")


if __name__ == "__main__":
    main()
