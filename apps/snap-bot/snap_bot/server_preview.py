"""Manual server preview, owner approval and permanent single-attempt fencing.

The ledger is local to this runtime; GitHub authority is never synthesized.
"""

import argparse
import hashlib
import json
import os
import pwd
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .actions_preview import OWNER, ROOT, digest_guard
from .actions_preview import render_bundle as shared_render_bundle
from .send_evidence import failure_receipt, network_started
from .send_reconciliation import receipt_allows
from .telegram_egress import DeliveryConfirmed, deliver

STATE = Path("/var/lib/madar/snap")

RENDER_WORK = Path("/var/lib/madar/render-work")


def render_bundle(output, provenance):
    """Keep the existing renderer non-root even when the controller is root."""
    if os.geteuid() != 0:
        return shared_render_bundle(output, provenance)
    import shutil
    import tempfile

    _, uid = source_info()
    account = pwd.getpwuid(uid)
    base = safe_path(RENDER_WORK)
    if not base.is_dir() or base.stat().st_uid != 0 or base.stat().st_mode & 0o022:
        raise ValueError("Trusted render staging directory required")
    with tempfile.TemporaryDirectory(prefix="render-", dir=base) as temporary:
        work = Path(temporary)
        os.chown(work, uid, account.pw_gid)
        bundle = work / "bundle"
        code = "import json,sys,os; assert os.geteuid()!=0; from snap_bot.actions_preview import render_bundle; render_bundle(sys.argv[1],json.loads(sys.argv[2]))"
        env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
        # A separate manager-created coordinator does not inherit the root
        # controller's NoNewPrivileges. It needs the existing ubuntu sudo
        # permission solely to launch the shared non-root renderer sandbox.
        unit = "madar-render-coordinator-" + uuid.uuid4().hex
        command = [
            "systemd-run",
            "--quiet",
            "--collect",
            "--wait",
            "--pipe",
            "--unit=" + unit,
            "--property=User=" + str(uid),
            "--property=Group=" + str(account.pw_gid),
            "--property=PrivateNetwork=yes",
            "--property=RestrictAddressFamilies=AF_UNIX",
            "--property=RuntimeMaxSec=300",
            "--property=WorkingDirectory=" + str(ROOT),
            "--setenv=PYTHONPATH=" + str(ROOT / "apps/snap-bot"),
            "--setenv=PYTHONDONTWRITEBYTECODE=1",
            "--setenv=TMPDIR=" + str(work),
            sys.executable,
            "-c",
            code,
            str(bundle),
            json.dumps(provenance),
        ]
        subprocess.run(command, env=env, cwd=ROOT, capture_output=True, timeout=330, check=True)
        if Path(output).exists() or any(path.is_symlink() for path in bundle.rglob("*")):
            raise ValueError("Unsafe renderer output")
        # The controller owns the completed snapshot before Repository adjusts DB permissions.
        for path in (bundle, *bundle.rglob("*")):
            os.chown(path, 0, 0)
        data = json.loads((bundle / "preview.json").read_text())
        # Root-only preview parent prevents the non-root renderer from changing it later.
        shutil.move(str(bundle), str(output))
        sync_directory(Path(output).parent)
        return data


def sha(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError("Exact video SHA required")
    return value


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def source_info():
    manifest = ROOT / "madar-release.json"
    if manifest.exists():
        if manifest.is_symlink() or manifest.stat().st_uid != 0 or manifest.stat().st_mode & 0o022:
            raise ValueError("Untrusted release")
        data = json.loads(manifest.read_text())
        commit, uid = data["commit"], data["owner_uid"]
    else:
        commit = subprocess.check_output(
            ["git", "--no-optional-locks", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        uid = pwd.getpwnam("ubuntu").pw_uid
    if not re.fullmatch("[0-9a-f]{40}", commit) or type(uid) is not int or uid <= 0:
        raise ValueError("Invalid release authority")
    return commit, uid


def owner_actor(uid):
    raw = os.environ.get("SUDO_UID", "")
    if os.geteuid() != 0 or raw != str(uid):
        raise ValueError("Owner sudo session required")
    return uid


def safe_path(path):
    path = Path(path)
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("Symlink state rejected")
    return path


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def exclusive_json(path, value):
    safe_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def verify_quality(directory, data, expected_sha):
    """Reuse the existing content, digest and owned quality contracts."""
    import tomllib

    from studio_core.config import Config
    from studio_core.jobs import Repository

    from .did_you_know import validate_content
    from .owned_contract_adapter import quality_chain

    if any(p.is_symlink() for p in directory.rglob("*")):
        raise ValueError("Symlink bundle rejected")
    if (
        data.get("repository") != "Khalidshar/snap-bot-v2"
        or data.get("delivery") != "NOT_ATTEMPTED"
        or data.get("human_approval") is not None
    ):
        raise ValueError("Invalid unapproved preview")
    if (
        not re.fullmatch("[0-9a-f]{32}", data["job_id"])
        or data["attempt"] != 1
        or not 0 < time.time() < data["expires_at"]
    ):
        raise ValueError("Invalid or expired job")
    if digest(directory / "preview.png") != data["preview_sha256"]:
        raise ValueError("Preview changed")
    config = replace(
        Config.load(ROOT / "config/snap/did_you_know.toml"),
        data_root=directory / "state",
        owners={"telegram": (OWNER,)},
        lease_seconds=3600,
    )
    repo = Repository(config)
    repo.assert_owner(data["job_id"], data["lease_token"], attempt=data["attempt"])
    root = config.data_root / "renders" / data["job_id"]
    result = json.loads((root / "output/render-result.json").read_text())
    content = json.loads((root / "input/content.json").read_text())
    validate_content(content)
    env = {"VIDEO_SHA": expected_sha, "CONTENT_HASH": result["content_hash"]}
    digest_guard((root / "output/did-you-know.mp4").read_bytes(), result, content, env)
    digest_guard((directory / "did-you-know.mp4").read_bytes(), result, content, env)
    profile = tomllib.loads((ROOT / "config/snap/did_you_know.toml").read_text())["slice"][
        "profile"
    ]
    if result != data["render"] or result["font_sha256"] != profile["font_sha256"]:
        raise ValueError("Render profile changed")
    isolation = result.get("network_isolation", {})
    if (
        set(isolation) != {"ipv4", "ipv6", "udp", "egress_unreachable"}
        or not all(isolation.values())
        or content["expires_at"] <= time.time()
    ):
        raise ValueError("Isolation or content invalid")
    if (
        result.get("safe_area", {}).get("result") != "PASS"
        or result.get("qa", {}).get("result") != "PASS"
        or result.get("glyph_coverage") != "PASS"
    ):
        raise ValueError("Quality missing")
    with repo.connect() as db:
        if quality_chain(db, data["job_id"], data["attempt"], result) != data["quality"]:
            raise ValueError("Owned quality changed")
    return config, repo, data, result


class Runtime:
    # Alternate root is an internal test seam; CLI always uses STATE.
    def __init__(self, root=STATE):
        self.root = safe_path(root)

    @contextmanager
    def locked(self):
        import fcntl

        for directory in (self.root, self.root / "previews", self.root / "intents"):
            safe_path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(safe_path(self.root / "lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def events(self):
        path = safe_path(self.root / "events.jsonl")
        if not path.exists():
            return []
        raw = path.read_bytes()
        if len(raw) > 32_000_000 or (raw and not raw.endswith(b"\n")):
            raise ValueError("Incomplete ledger")
        events, previous = [], "0" * 64
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

    def append(self, kind, **fields):
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

    def spent(self, video_sha):
        return (self.root / "intents" / (video_sha + ".json")).exists() or any(
            x["kind"] == "INTENT" and x.get("video_sha") == video_sha for x in self.events()
        )

    def render(self):
        commit, _ = source_info()
        with self.locked():
            identifier = uuid.uuid4().hex
            directory = self.root / "previews" / identifier
            data = render_bundle(directory, {"run_id": identifier, "head_sha": commit})
            video_sha = sha(data["render"]["video_sha256"])
            if any(
                x["kind"] == "RENDERED" and x.get("video_sha") == video_sha for x in self.events()
            ):
                raise ValueError("SHA already rendered")
            evidence = {
                "preview_id": identifier,
                "video_sha": video_sha,
                "content_hash": data["render"]["content_hash"],
                "commit": commit,
                "bundle_sha": digest(directory / "preview.json"),
            }
            exclusive_json(directory / "evidence.json", evidence)
            self.append("RENDERED", **evidence, evidence_sha=digest(directory / "evidence.json"))
            return evidence

    def verify(self, video_sha):
        sha(video_sha)
        commit, _ = source_info()
        records = [
            x for x in self.events() if x["kind"] == "RENDERED" and x.get("video_sha") == video_sha
        ]
        if len(records) != 1:
            raise ValueError("Unique rendered evidence required")
        record = records[0]
        if not re.fullmatch("[0-9a-f]{32}", record["preview_id"]):
            raise ValueError("Invalid preview ID")
        directory = safe_path(self.root / "previews" / record["preview_id"])
        if any(p.is_symlink() for p in directory.rglob("*")):
            raise ValueError("Symlink bundle rejected")
        if digest(directory / "did-you-know.mp4") != video_sha:
            raise ValueError("Video SHA changed")
        if (
            digest(directory / "evidence.json") != record["evidence_sha"]
            or digest(directory / "preview.json") != record["bundle_sha"]
            or record["commit"] != commit
        ):
            raise ValueError("Evidence or release changed")
        data = json.loads((directory / "preview.json").read_text())
        if (
            data["head_sha"] != commit
            or data["run_id"] != record["preview_id"]
            or data["render"]["content_hash"] != record["content_hash"]
        ):
            raise ValueError("Provenance changed")
        return directory, record, verify_quality(directory, data, video_sha)

    def approve(self, video_sha):
        _, uid = source_info()
        actor = owner_actor(uid)
        with self.locked():
            directory, record, (_, repo, data, result) = self.verify(video_sha)
            if self.spent(video_sha) or any(
                x["kind"] == "HUMAN_FRAME" and x.get("video_sha") == video_sha
                for x in self.events()
            ):
                raise ValueError("Approval cannot replace prior decision")
            from .final_agent import evaluate_final, record_human_frame

            record_human_frame(
                repo, data["job_id"], data["lease_token"], data["attempt"], result, reviewer=OWNER
            )
            if (
                evaluate_final(repo, data["job_id"], data["lease_token"], data["attempt"], result)[
                    "delivery_decision"
                ]
                != "APPROVED"
            ):
                raise ValueError("Final blocked")
            return self.append(
                "HUMAN_FRAME",
                video_sha=video_sha,
                owner_uid=actor,
                preview_id=record["preview_id"],
                content_hash=record["content_hash"],
                commit=record["commit"],
                evidence_sha=record["evidence_sha"],
            )

    def require_approval(self, record, uid):
        keys = ("video_sha", "preview_id", "content_hash", "commit", "evidence_sha")
        if not any(
            x["kind"] == "HUMAN_FRAME"
            and x.get("owner_uid") == uid
            and all(x.get(k) == record[k] for k in keys)
            for x in self.events()
        ):
            raise ValueError("Matching HUMAN_FRAME required")

    def send(self, video_sha, transport=None):
        sha(video_sha)
        _, uid = source_info()
        with self.locked():
            if self.spent(video_sha):
                raise ValueError("SHA permanently consumed")
            directory, record, (config, _, data, _) = self.verify(video_sha)
            self.require_approval(record, uid)
            intent = {
                "run_id": record["preview_id"],
                "video_sha": video_sha,
                "content_hash": record["content_hash"],
                "id": uuid.uuid4().hex,
                "state": "EXTERNAL_EFFECT_INTENT",
            }
            exclusive_json(self.root / "intents" / (video_sha + ".json"), intent)
            self.append("INTENT", **intent)
            reset = network_started.set(False)
            try:
                receipt = deliver(
                    config,
                    data["job_id"],
                    video_sha,
                    transport=transport,
                    lease_token=data["lease_token"],
                    review_mode="HUMAN_FRAME",
                )
                if receipt.get("delivery") != "PUBLISHED":
                    raise RuntimeError("Delivery not confirmed")
                return self.append(
                    "ACK", video_sha=video_sha, message_id=receipt["message_id"], bot=receipt["bot"]
                )
            except DeliveryConfirmed:
                self.append("ACK_EVIDENCE_PENDING", video_sha=video_sha)
                raise RuntimeError("Confirmed delivery requires local reconciliation") from None
            except Exception:
                proof = failure_receipt(
                    directory / "server-failure.json",
                    run_id=intent["run_id"],
                    video_sha=video_sha,
                    intent_id=intent["id"],
                    content_hash=intent["content_hash"],
                    reason="SERVER_SEND_FAILED",
                )
                proven = receipt_allows(proof, {"id": intent["id"]}, intent)
                self.append("PRE_NETWORK_BLOCKED" if proven else "UNKNOWN", video_sha=video_sha)
                raise RuntimeError("SHA permanently blocked; no retry") from None
            finally:
                network_started.reset(reset)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("render", "approve", "send"))
    parser.add_argument("--video-sha")
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ValueError("Root controller required")
        runtime = Runtime()
        value = (
            runtime.render()
            if args.action == "render"
            else getattr(runtime, args.action)(sha(args.video_sha))
        )
        print(json.dumps(value, ensure_ascii=True))
    except Exception:
        print("SERVER_PREVIEW_BLOCKED; inspect durable local evidence", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
