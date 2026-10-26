"""Manual Status server cycle: live weather, no-network render, owner approval, one send.

Each run fetches the weather through its own transient egress unit (the only process with IP
sockets, exact allowlisted hosts), renders in a transient unit without network, then records
the validated preview under the state directory with an append-only ledger.

Delivery mirrors the Snap server cycle: the owner approves one exact video SHA on the server,
stating how many hours ago they last messaged the business number (WhatsApp only lets a
business send a video inside 24 hours of the owner's last message; with no webhook here the
owner's statement is that evidence, and Graph still refuses a closed window). The send unit
then signs a short review, prepares the outbox, uploads and makes at most one messages POST.
Each SHA is sent at most once; an unknown outcome is never retried.
Usage (root, from the release venv):
  python -m status_bot.server_preview render
  python -m status_bot.server_preview approve --video-sha <sha> --messaged-hours-ago <0-23>
  python -m status_bot.server_preview send --video-sha <sha>     (madar-v3-status-send@<sha>)
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
from studio_core.jobs import Repository
from studio_core.network import NetworkClient
from studio_core.storage import Storage

from .pipeline import prepare, record_validation
from .whatsapp_contract import DeliveryIdentity, sign_review
from .whatsapp_contract import sha as contract_sha
from .whatsapp_egress import DeliveryWorker, GraphCredentials, GraphTransport
from .whatsapp_outbox import WhatsAppOutbox

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
GRAPH_VERSION = "v21.0"
REVIEW_SECONDS = 300
WINDOW_SECONDS = 86400
# The send unit's EnvironmentFile; values are read, never printed.
WA_ENV = (
    "WA_TOKEN",
    "STATUS_WA_BUSINESS_ACCOUNT",
    "STATUS_WA_PHONE_NUMBER",
    "STATUS_WA_RECIPIENT",
    "STATUS_WA_GATE_KEY",
    "STATUS_WA_WEBHOOK_KEY",
)
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
        # The job stays owned for the content's hour, so the owner can review and send.
        "lease_seconds = 600": "lease_seconds = 3600",
        "NORMAL = 600": "NORMAL = 3600",
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
            # The lease stays root-only beside the preview: the send unit needs it to own the job.
            claim = prepared["claim"]
            lease = {
                "run": identifier[:12],
                "lease_token": claim.get("lease_token"),
                "attempt": claim.get("attempt", 1),
            }
            fd = os.open(preview / "lease.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(lease, stream)
            sync_directory(preview)
            self.append("RENDERED", **evidence, evidence_sha=digest(preview / "evidence.json"))
            return {
                **evidence,
                "qa": result["qa"],
                "network_isolation": result["network_isolation"],
            }

    def rendered(self, video_sha: str) -> dict[str, Any]:
        if not isinstance(video_sha, str) or not re.fullmatch("[0-9a-f]{64}", video_sha):
            raise ValueError("Exact video SHA required")
        records = [
            x for x in self.events() if x["kind"] == "RENDERED" and x.get("video_sha") == video_sha
        ]
        if len(records) != 1:
            raise ValueError("Unknown or ambiguous SHA")
        record = records[0]
        if not re.fullmatch("[0-9a-f]{32}", record["preview_id"]):
            raise ValueError("Invalid preview")
        preview = safe_path(self.root / "previews" / record["preview_id"])
        if digest(preview / "weather.mp4") != video_sha:
            raise ValueError("Preview changed")
        return record

    def spent(self, video_sha: str) -> bool:
        return any(x["kind"] == "INTENT" and x.get("video_sha") == video_sha for x in self.events())

    def approve(self, video_sha: str, hours_ago: int) -> dict[str, Any]:
        """The owner watched this exact video and messaged the number `hours_ago` hours ago."""
        if type(hours_ago) is not int or not 0 <= hours_ago <= 23:
            raise ValueError("Last message to the number must be 0-23 hours ago")
        with self.locked():
            record = self.rendered(video_sha)
            now = time.time()
            if self.spent(video_sha):
                raise ValueError("SHA already sent")
            if any(
                x["kind"] == "HUMAN_FRAME" and x.get("video_sha") == video_sha
                for x in self.events()
            ):
                raise ValueError("SHA already approved")
            if not now < record["expires_at"]:
                raise ValueError("Content expired")
            return self.append(
                "HUMAN_FRAME",
                video_sha=video_sha,
                preview_id=record["preview_id"],
                channel="whatsapp",
                inbound_at=now - hours_ago * 3600,
            )

    def send(
        self,
        video_sha: str,
        transport: Any = None,
        environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        env = dict(os.environ if environment is None else environment)
        with self.locked():
            record = self.rendered(video_sha)
            approvals = [
                x
                for x in self.events()
                if x["kind"] == "HUMAN_FRAME" and x.get("video_sha") == video_sha
            ]
            if len(approvals) != 1 or approvals[0].get("channel") != "whatsapp":
                raise ValueError("Owner approval required")
            if self.spent(video_sha):
                raise ValueError("SHA already sent")
            now = time.time()
            inbound = approvals[0]["inbound_at"]
            if not 0 <= now - inbound < WINDOW_SECONDS:
                raise ValueError("WhatsApp window closed: message the number, then render again")
            missing = [name for name in WA_ENV if not env.get(name)]
            if missing:
                raise ValueError("WhatsApp environment incomplete: " + ",".join(missing))
            identity = DeliveryIdentity(
                env["STATUS_WA_BUSINESS_ACCOUNT"],
                env["STATUS_WA_PHONE_NUMBER"],
                env["STATUS_WA_RECIPIENT"],
                env["STATUS_WA_GATE_KEY"].encode(),
                env["STATUS_WA_WEBHOOK_KEY"].encode(),
            )
            preview = safe_path(self.root / "previews" / record["preview_id"])
            lease = json.loads((preview / "lease.json").read_text())
            if not re.fullmatch("[0-9a-f]{12}", str(lease.get("run"))):
                raise ValueError("Invalid run")
            run = safe_path(self.root / "runs" / lease["run"])
            repo = Repository(Config.load(run / "weather.toml"))
            outbox = WhatsAppOutbox(repo, identity)
            job, attempt, token = record["job_id"], lease["attempt"], lease["lease_token"]
            manifest, _ = outbox.bundle(job, attempt)
            review = {
                "app": "status-bot-v2",
                "job_id": job,
                "attempt": attempt,
                "content_hash": manifest["content"]["content_hash"],
                "video_sha256": video_sha,
                "manifest_sha256": contract_sha(manifest),
                "identity_binding": identity.binding,
                "decision": "APPROVE_WHATSAPP_VIDEO",
                "issued_at": now,
                "expires_at": now + REVIEW_SECONDS,
                "nonce": uuid.uuid4().hex,
            }
            signature = sign_review(review, identity.gate_key)
            receipt = outbox.prepare(job, token, attempt, review, signature)
            with repo.transaction() as db:
                # The owner's stated last message stands in for the inbound webhook.
                db.execute(
                    "INSERT INTO whatsapp_windows VALUES(?,?) ON CONFLICT(identity_binding) "
                    "DO UPDATE SET last_inbound_at=MAX(last_inbound_at,excluded.last_inbound_at)",
                    (identity.binding, inbound),
                )
            fields = {"video_sha": video_sha, "outbox_id": receipt["id"], "channel": "whatsapp"}
            self.append("INTENT", **fields)
            if transport is None:
                credentials = GraphCredentials(
                    GRAPH_VERSION, identity.phone_number, identity.recipient, env["WA_TOKEN"]
                )
                transport = GraphTransport(credentials, enabled=True)
            try:
                result = DeliveryWorker(outbox, transport, enabled=True).deliver(
                    receipt["id"], token, review, signature
                )
            except Exception as error:
                # The outbox refused before its upload began: nothing reached Graph.
                self.append("PRE_NETWORK_BLOCKED", **fields, code=type(error).__name__)
                raise RuntimeError("Blocked before WhatsApp; SHA consumed, render again") from None
            if result["state"] == "API_ACCEPTED":
                return self.append("ACK", **fields, state=result["state"])
            if result["state"] == "REJECTED":
                self.append("REJECTED", **fields, code=result["error_code"])
                raise RuntimeError("WhatsApp refused the message: " + str(result["error_code"]))
            self.append("UNKNOWN", **fields, state=result["state"])
            raise RuntimeError("WhatsApp outcome unknown; SHA permanently blocked, no retry")


def state_root(value: str | None) -> Path:
    if value is None:
        return STATE
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or Path("/var/lib") not in path.parents:
        raise ValueError("State root must be under /var/lib")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("render", "approve", "send"))
    parser.add_argument("--video-sha", default="")
    parser.add_argument("--messaged-hours-ago", type=int)
    parser.add_argument("--state", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ValueError("Root controller required")
        runtime = Runtime(state_root(args.state))
        if args.action == "render":
            value = runtime.render()
        elif args.action == "approve":
            if args.messaged_hours_ago is None:
                raise ValueError("--messaged-hours-ago is required")
            value = runtime.approve(args.video_sha, args.messaged_hours_ago)
        else:
            value = runtime.send(args.video_sha)
        print(json.dumps(value, ensure_ascii=False))
    except Exception as error:
        # No secrets exist in this path; the reason is needed to diagnose a blocked run.
        print(
            "STATUS_PREVIEW_BLOCKED: " + type(error).__name__ + ": " + str(error), file=sys.stderr
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
