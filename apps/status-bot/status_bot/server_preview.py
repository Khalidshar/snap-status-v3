"""Manual Status server preview: live weather, no-network render, local owner preview.

Each run fetches the weather through its own transient egress unit (the only process with IP
sockets, exact allowlisted hosts), renders in a transient unit without network, then records
the validated preview under the state directory with an append-only ledger. There is no
WhatsApp or Telegram path here: delivery is a separate, later owner decision.
Usage (root, from the release venv):
  python -m status_bot.server_preview render
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from studio_core.config import Config
from studio_core.network import NetworkClient
from studio_core.storage import Storage

from .pipeline import prepare, record_validation

ROOT = Path(__file__).resolve().parents[3]
STATE = Path("/var/lib/madar/status")
CONFIG = "config/status/weather.toml"
OUTPUTS = ("weather.mp4", "preview.png", "render-result.json")
RUNNER = """import json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text())
from studio_core.config import Config
from status_bot.render import render
cfg=Config.load(Path(p['config']))
print(json.dumps(render(cfg,p['settings'],p['job'],p['digest'])))
"""
TOOLS = (("ffmpeg", "ffmpeg"), ("ffprobe", "ffprobe"), ("fc_query", "fc-query"))
COMMON = (
    "--property=NoNewPrivileges=yes",
    "--property=CapabilityBoundingSet=",
    "--property=KillMode=control-group",
    "--property=ProtectSystem=strict",
    "--property=ProtectHome=read-only",
    "--property=PrivateTmp=yes",
    "--property=PrivateDevices=yes",
    "--property=MemoryMax=1G",
    "--setenv=PYTHONDONTWRITEBYTECODE=1",
)


def safe_path(path: str | Path) -> Path:
    path = Path(path)
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("Symlink state rejected")
    return path


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def release_commit() -> str:
    manifest = ROOT / "madar-release.json"
    if manifest.exists():
        if manifest.is_symlink() or manifest.stat().st_uid != 0 or manifest.stat().st_mode & 0o022:
            raise ValueError("Untrusted release")
        commit = json.loads(manifest.read_text())["commit"]
    else:
        commit = subprocess.check_output(
            ["git", "--no-optional-locks", "rev-parse", "HEAD"],  # noqa: S607  # pinned system tool
            cwd=ROOT,
            text=True,
        ).strip()
    if not isinstance(commit, str) or not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError("Invalid release authority")
    return commit


def run_config(run: Path) -> Path:
    """Copy the pinned profile, pointing its data and egress socket into this run only."""
    text = (ROOT / CONFIG).read_text(encoding="utf-8")
    replacements = {
        'data_root = "../.status-weather-state"': 'data_root = "' + str(run / "data") + '"',
        'socket = "egress.sock"': 'socket = "' + str(run / "egress.sock") + '"',
    }
    for old, new in replacements.items():
        if text.count(old) != 1:
            raise ValueError("Unexpected Status profile")
        text = text.replace(old, new)
    path = run / "weather.toml"
    path.write_text(text, encoding="utf-8")
    return path


def settings_for(path: Path, run: Path) -> dict[str, Any]:
    settings: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))["slice"]
    profile = settings["profile"]
    for name, tool in TOOLS:
        found = shutil.which(tool, path="/usr/bin:/bin")
        if not found or found != profile.get(name, found):
            raise ValueError("Pinned tool missing: " + tool)
        profile[name] = found
    # Font hashes stay pinned by the profile; render refuses a different font file.
    settings["egress_socket"] = str(run / "egress.sock")
    return settings


def systemctl(*args: str) -> None:
    subprocess.run(  # noqa: S603  # fixed argv, no shell
        ["systemctl", *args],  # noqa: S607  # pinned system tool
        check=False,
        capture_output=True,
        timeout=15,
    )


class Runtime:
    # Alternate root is a test seam for disposable runners; the unit always uses STATE.
    def __init__(self, root: str | Path = STATE) -> None:
        self.root = safe_path(root)

    @contextmanager
    def locked(self) -> Iterator[None]:
        import fcntl

        for directory in (self.root, self.root / "previews", self.root / "runs"):
            safe_path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(safe_path(self.root / "lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def events(self) -> list[dict[str, Any]]:
        path = safe_path(self.root / "events.jsonl")
        if not path.exists():
            return []
        raw = path.read_bytes()
        if len(raw) > 32_000_000 or (raw and not raw.endswith(b"\n")):
            raise ValueError("Incomplete ledger")
        events: list[dict[str, Any]] = []
        previous = "0" * 64
        for line in raw.splitlines():
            value = json.loads(line)
            signature = value.pop("hash")
            if (
                value["seq"] != len(events) + 1
                or value["previous"] != previous
                or hashlib.sha256(canonical(value)).hexdigest() != signature
            ):
                raise ValueError("Corrupt ledger")
            value["hash"] = signature
            events.append(value)
            previous = signature
        return events

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        events = self.events()
        value = dict(
            fields,
            kind=kind,
            time=time.time(),
            seq=len(events) + 1,
            previous=events[-1]["hash"] if events else "0" * 64,
        )
        value["hash"] = hashlib.sha256(canonical(value)).hexdigest()
        path = safe_path(self.root / "events.jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(self.root)
        return value

    def start_egress(self, unit: str, config: Path, socket_path: Path) -> None:
        command = [
            "systemd-run",
            "--quiet",
            "--unit",
            unit,
            "--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "--property=ReadWritePaths=" + str(socket_path.parent),
            "--property=RuntimeMaxSec=300",
            *COMMON,
            sys.executable,
            "-m",
            "studio_core.cli",
            "--config",
            str(config),
            "proxy",
        ]
        subprocess.run(command, check=True, capture_output=True, timeout=30)  # noqa: S603  # fixed argv
        if len(os.fsencode(socket_path)) > 107:
            raise ValueError("Egress socket path too long")
        deadline = time.monotonic() + 20
        while not socket_path.is_socket():
            if time.monotonic() > deadline:
                raise RuntimeError("Egress unit did not start")
            time.sleep(0.2)

    def render_unit(self, unit: str, run: Path, payload: Path) -> dict[str, Any]:
        runner = run / "runner.py"
        runner.write_text(RUNNER)
        command = [
            "systemd-run",
            "--quiet",
            "--wait",
            "--pipe",
            "--unit",
            unit,
            "--property=Type=oneshot",
            "--property=PrivateNetwork=yes",
            "--property=RestrictAddressFamilies=AF_UNIX",
            "--property=TimeoutStartSec=300s",
            "--property=TimeoutStopSec=5s",
            "--property=ReadWritePaths=" + str(run / "data"),
            "--property=InaccessiblePaths=" + str(run / "egress.sock"),
            *COMMON,
            sys.executable,
            str(runner),
            str(payload),
        ]
        result = subprocess.run(  # noqa: S603  # fixed argv
            command, capture_output=True, text=True, timeout=320, check=False
        )
        if result.returncode:
            raise RuntimeError("Render unit failed: " + result.stderr[-2000:])
        return next(
            json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"job_id"')
        )

    def render(self) -> dict[str, Any]:
        commit = release_commit()
        with self.locked():
            identifier = uuid.uuid4().hex
            # Short run directory: the egress socket path must fit AF_UNIX's 107 bytes.
            run = safe_path(self.root / "runs" / identifier[:12])
            run.mkdir(mode=0o700)
            (run / "data").mkdir(mode=0o700)
            egress = "madar-v3-status-egress-" + identifier[:12] + ".service"
            renderer = "madar-v3-status-render-" + identifier[:12] + ".service"
            started = False
            try:
                config_path = run_config(run)
                cfg = Config.load(config_path)
                settings = settings_for(config_path, run)
                started = True
                self.start_egress(egress, config_path, cfg.proxy_socket)
                prepared = prepare(
                    cfg,
                    settings,
                    NetworkClient(cfg),
                    secrets.token_hex(32),
                    "server-" + identifier,
                    synthetic=False,
                )
                job = prepared["claim"]["id"]
                payload = run / "request.json"
                request = {
                    "config": str(config_path),
                    "settings": settings,
                    "job": job,
                    "digest": prepared["content"]["content_hash"],
                }
                payload.write_text(json.dumps(request, ensure_ascii=False))
                result = self.render_unit(renderer, run, payload)
                manifest = record_validation(cfg, prepared, result)
            finally:
                if started:
                    for name in (renderer, egress):
                        systemctl("stop", name)
                        systemctl("reset-failed", name)
            preview = safe_path(self.root / "previews" / identifier)
            preview.mkdir(mode=0o700)
            workspace = Storage(cfg).workspace(job) / "output"
            for name in OUTPUTS:
                source = workspace / name
                if source.is_symlink() or not source.is_file():
                    raise ValueError("Missing render output")
                shutil.copyfile(source, preview / name)
            (preview / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2)
            )
            video_sha = digest(preview / "weather.mp4")
            if video_sha != result["video_sha256"]:
                raise ValueError("Preview changed")
            evidence = {
                "preview_id": identifier,
                "job_id": job,
                "video_sha": video_sha,
                "preview_sha": digest(preview / "preview.png"),
                "content_hash": result["content_hash"],
                "source": prepared["content"]["source"],
                "commit": commit,
                "expires_at": prepared["content"]["expires_at"],
                "delivery": "NOT_ATTEMPTED",
            }
            (preview / "evidence.json").write_text(json.dumps(evidence, indent=2))
            sync_directory(preview)
            self.append("RENDERED", **evidence, evidence_sha=digest(preview / "evidence.json"))
            return {
                **evidence,
                "qa": result["qa"],
                "network_isolation": result["network_isolation"],
            }


def state_root(value: str | None) -> Path:
    if value is None:
        return STATE
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or Path("/var/lib") not in path.parents:
        raise ValueError("State root must be under /var/lib")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("render",))
    parser.add_argument("--state", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ValueError("Root controller required")
        value = Runtime(state_root(args.state)).render()
        print(json.dumps(value, ensure_ascii=False))
    except Exception as error:
        # No secrets exist in this path; the reason is needed to diagnose a blocked run.
        print(
            "STATUS_PREVIEW_BLOCKED: " + type(error).__name__ + ": " + str(error), file=sys.stderr
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
