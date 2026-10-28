"""Disposable GitHub Actions preview and explicitly approved test transport.

No scheduler; render mode never imports delivery or creates a human approval.
Only the send command may use existing TG_TEST_TOKEN/TG_TEST_CHAT.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
REPOSITORY = "Khalidshar/snap-bot-v2"
OWNER = "Khalidshar"
RENDER_WORKFLOW = ".github/workflows/did-you-know-manual.yml"
ARTIFACT = "madar-did-you-know-test"


def approval_guard(env: Mapping[str, str]) -> None:
    if env.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("Wrong repository")
    if (
        env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or env.get("GITHUB_REF") != "refs/heads/main"
    ):
        raise ValueError("Manual main dispatch required")
    if (
        env.get("GITHUB_ACTOR", "").lower() != OWNER.lower()
        or env.get("GITHUB_TRIGGERING_ACTOR", "").lower() != OWNER.lower()
    ):
        raise ValueError("Khalid dispatch required")
    if env.get("HUMAN_APPROVAL") != "HUMAN_FRAME":
        raise ValueError("Explicit HUMAN_FRAME required")
    for key in ("VIDEO_SHA", "CONTENT_HASH"):
        if not re.fullmatch("[0-9a-f]{64}", env.get(key, "")):
            raise ValueError("Explicit digest required")
    if not re.fullmatch("[1-9][0-9]{0,19}", env.get("SOURCE_RUN", "")):
        raise ValueError("Source run required")


def source_guard(run: Any, env: Mapping[str, str], now: float | None = None) -> None:
    now = time.time() if now is None else now
    if (
        run.get("event") != "workflow_dispatch"
        or run.get("path") != RENDER_WORKFLOW
        or run.get("conclusion") != "success"
    ):
        raise ValueError("Source is not successful manual render")
    if str(run.get("id")) != env["SOURCE_RUN"] or run.get("head_sha") != env["GITHUB_SHA"]:
        raise ValueError("Source commit/run changed")
    if run.get("repository", {}).get("full_name") != REPOSITORY:
        raise ValueError("Foreign source")
    from datetime import datetime

    created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).timestamp()
    if not 0 <= now - created < 3600:
        raise ValueError("Source expired")


def history_guard(artifacts: Any, video_sha: str, env: Mapping[str, str] | None = None) -> None:
    from .send_reconciliation import load_entries, resolve_prior

    name = "did-you-know-send-attempt-" + video_sha
    for item in artifacts:
        if item.get("name") != name:
            continue
        if env is None or not resolve_prior(item, env, github_get, load_entries(ROOT)):
            raise ValueError("Prior send intent exists; unknown outcomes never retry")


def intent_guard(env: Mapping[str, str], artifact: Any) -> None:
    if not re.fullmatch("[1-9][0-9]*", env.get("SEND_INTENT_ARTIFACT_ID", "")):
        raise ValueError("Durable intent ID missing")
    if (
        str(artifact.get("id")) != env["SEND_INTENT_ARTIFACT_ID"]
        or artifact.get("name") != "did-you-know-send-attempt-" + env["VIDEO_SHA"]
        or artifact.get("expired") is not False
        or str(artifact.get("workflow_run", {}).get("id")) != env["GITHUB_RUN_ID"]
    ):
        raise ValueError("Durable intent binding invalid")


def github_get(path: str, env: Mapping[str, str]) -> Any:
    from urllib.request import Request, urlopen

    token = env.get("GITHUB_TOKEN", "")
    if not token:
        raise ValueError("Actions read credential unavailable")
    request = Request(
        "https://api.github.com/repos/" + REPOSITORY + "/" + path,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310  # fixed https URL
        raw = response.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("GitHub response too large")
    return json.loads(raw)


def preflight(env: Mapping[str, str]) -> None:
    approval_guard(env)
    source_guard(github_get("actions/runs/" + env["SOURCE_RUN"], env), env)
    current = github_get("actions/runs/" + env["GITHUB_RUN_ID"], env)
    if (
        current.get("event") != "workflow_dispatch"
        or current.get("actor", {}).get("login", "").lower() != OWNER.lower()
        or current.get("triggering_actor", {}).get("login", "").lower() != OWNER.lower()
    ):
        raise ValueError("Authenticated Khalid dispatch missing")
    for page in range(1, 101):
        items = github_get("actions/artifacts?per_page=100&page=" + str(page), env)["artifacts"]
        history_guard(items, env["VIDEO_SHA"], env)
        if len(items) < 100:
            return
    raise ValueError("History scan incomplete")


def new_output_directory(output: Path) -> None:
    output.parent.mkdir(exist_ok=True)
    output.mkdir(mode=0o750)


def render_bundle(output: Path, provenance: Any) -> Any:
    """Actual owned pipeline, renderer systemd sandbox and SQLite backup."""
    from studio_core.config import Config
    from studio_core.jobs import Repository
    from studio_core.jobs.gateway import CommandGateway

    from .owned_contract_adapter import quality_chain
    from .service import execute

    if os.name != "posix":
        raise RuntimeError("Linux systemd required; no isolation fallback")
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Output must be new")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="madar-manual-") as d:
        runtime = Path(d)
        state = runtime / "state"
        spool = runtime / "spool/renders"
        inputs = runtime / "inputs/renders"
        socket_dir = runtime / "run"
        for path in (spool, inputs, socket_dir):
            path.mkdir(parents=True, mode=0o750)
        text = (ROOT / "config/snap/did_you_know.toml").read_text(encoding="utf-8")
        values = {
            "data_root": str(state),
            "input_root": str(inputs),
            "spool_root": str(spool),
            "render_socket": str(socket_dir / "render.sock"),
            "egress_socket": str(runtime / "denied-egress.sock"),
            "font": str(ROOT / "assets/fonts/notokufiarabic/NotoKufiArabic-Bold.ttf"),
            "latin_font": str(ROOT / "assets/fonts/cairo/Cairo-Regular.ttf"),
            "font_manifest": str(ROOT / "assets/fonts/manifest.json"),
            "ffmpeg_version": subprocess.run(
                ["/usr/bin/ffmpeg", "-version"], capture_output=True, text=True, check=True
            ).stdout.splitlines()[0],
        }
        for key, value in values.items():
            text = re.sub(r"^" + key + r" = .*$", key + " = " + json.dumps(value), text, flags=re.M)
        text = (
            text.replace("manual-test-owner", OWNER)
            .replace("lease_seconds = 30", "lease_seconds = 3600")
            .replace("NORMAL = 600", "NORMAL = 3600")
        )
        for key in ("worker_uid", "renderer_uid"):
            text = re.sub(r"^" + key + r" = .*$", key + " = " + str(os.getuid()), text, flags=re.M)
        path = runtime / "config.toml"
        path.write_text(text, encoding="utf-8")
        config = Config.load(path)
        settings = tomllib.loads(text)["slice"]
        from .did_you_know import DEFAULT_KIND, KINDS

        kind = (
            provenance.get("kind", DEFAULT_KIND) if isinstance(provenance, dict) else DEFAULT_KIND
        )
        if kind not in KINDS:
            raise ValueError("Unknown content kind")
        settings["kind"] = kind
        repo = Repository(config)
        history = provenance.get("fact_history", []) if isinstance(provenance, dict) else []
        if history:
            from .did_you_know import seed_history

            with repo.transaction() as db:
                seed_history(db, history)
        local_nonce = uuid.uuid4().hex
        gateway = CommandGateway(config, repo, local_nonce)
        now = time.time()
        command = {
            "channel": "telegram",
            "actor": OWNER,
            "event_id": uuid.uuid4().hex,
            "command": "did_you_know",
            "priority": "NORMAL",
        }
        job, _ = gateway.accept(command, now, gateway.signature(command, now))
        claim: Any = repo.claim(job_id=job["id"])
        unit = "madar-manual-render-" + uuid.uuid4().hex
        properties = [
            "User=" + str(os.getuid()),
            "Group=" + str(os.getgid()),
            "ProtectSystem=strict",
            "NoNewPrivileges=yes",
            "RestrictAddressFamilies=AF_UNIX",
            "ReadWritePaths=" + str(spool) + " " + str(socket_dir),
            "InaccessiblePaths=" + str(state),
            "RuntimeMaxSec=300",
        ]
        args = ["sudo", "-n", "systemd-run", "--quiet", "--collect", "--unit=" + unit]
        for value in properties:
            args += ["--property=" + value]
        args += [
            "--setenv=PYTHONPATH=" + str(ROOT / "apps/snap-bot"),
            "--setenv=PYTHONDONTWRITEBYTECODE=1",
            sys.executable,
            "-m",
            "snap_bot.service",
            "--config",
            str(path),
            "renderer",
        ]
        try:
            subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)  # noqa: S603  # fixed argv, no shell
            deadline = time.monotonic() + 20
            while not Path(settings["render_socket"]).exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("Sandbox renderer did not start")
                time.sleep(0.1)
            result = execute(config, settings, local_nonce, claim=claim)
            if (
                result["external_delivery"]
                or result["final_decision"]["delivery_decision"] != "BLOCKED"
            ):
                raise ValueError("Preview cannot approve or send")
            render = result["render"]
            with repo.connect() as db:
                evidence = quality_chain(db, job["id"], claim["attempt"], render)
            new_output_directory(output)
            shutil.copytree(
                state, output / "state", ignore=shutil.ignore_patterns("jobs.sqlite*", "*.sock")
            )
            with repo.connect() as source, sqlite3.connect(output / "state/jobs.sqlite") as target:
                source.backup(target)
            root = state / "renders" / job["id"]
            shutil.copy(root / "output/did-you-know.mp4", output / "did-you-know.mp4")
            from .render import file_sha256

            preview = spool / job["id"] / "attempts" / str(claim["attempt"]) / "output/preview.png"
            preview_sha = file_sha256(preview)
            shutil.copy(preview, output / "preview.png")
            metadata = {
                "schema": 1,
                "repository": REPOSITORY,
                "run_id": str(provenance["run_id"]),
                "head_sha": provenance["head_sha"],
                "job_id": job["id"],
                "attempt": claim["attempt"],
                "lease_token": claim["lease_token"],
                "render": render,
                "expires_at": min(
                    repo.get(job["id"])["expires_at"],  # type: ignore[index]
                    json.loads((root / "input/content.json").read_text(encoding="utf-8"))[
                        "expires_at"
                    ],
                ),
                "quality": evidence,
                "preview_sha256": preview_sha,
                "delivery": "NOT_ATTEMPTED",
                "human_approval": None,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            (output / "preview.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return metadata
        finally:
            subprocess.run(  # noqa: S603  # fixed argv, no shell
                ["sudo", "-n", "systemctl", "stop", unit],  # noqa: S607  # pinned system tool
                capture_output=True,
                timeout=30,
            )


def digest_guard(video: bytes, result: Any, content: Any, env: Mapping[str, str]) -> None:
    if (
        not 0 < len(video) <= 50_000_000
        or hashlib.sha256(video).hexdigest() != env["VIDEO_SHA"]
        or result["video_sha256"] != env["VIDEO_SHA"]
    ):
        raise ValueError("Approved video differs")
    if (
        content["content_hash"] != env["CONTENT_HASH"]
        or result["content_hash"] != env["CONTENT_HASH"]
    ):
        raise ValueError("Approved content differs")


def verify_bundle(directory: str | Path, env: Mapping[str, str]) -> tuple[Any, Any, Any, Any]:
    approval_guard(env)
    from studio_core.config import Config
    from studio_core.jobs import Repository

    from .did_you_know import validate_content
    from .owned_contract_adapter import quality_chain

    directory = Path(directory).resolve()
    if any(p.is_symlink() for p in directory.rglob("*")):
        raise ValueError("Symlink bundle rejected")
    data = json.loads((directory / "preview.json").read_text(encoding="utf-8"))
    if (
        data.get("repository") != REPOSITORY
        or data.get("run_id") != env["SOURCE_RUN"]
        or data.get("head_sha") != env["GITHUB_SHA"]
    ):
        raise ValueError("Bundle provenance mismatch")
    if data.get("human_approval") is not None or data.get("delivery") != "NOT_ATTEMPTED":
        raise ValueError("Bundle was not unapproved preview")
    if not 0 < time.time() < data["expires_at"]:
        raise ValueError("Bundle expired")
    if (
        hashlib.sha256((directory / "preview.png").read_bytes()).hexdigest()
        != data["preview_sha256"]
    ):
        raise ValueError("Preview changed")
    if not re.fullmatch("[0-9a-f]{32}", data["job_id"]) or data["attempt"] != 1:
        raise ValueError("Bundle job/attempt invalid")
    config = replace(
        Config.load(ROOT / "config/snap/did_you_know.toml"),
        data_root=directory / "state",
        owners={"telegram": (OWNER,)},
        lease_seconds=3600,
    )
    repo = Repository(config)
    repo.assert_owner(data["job_id"], data["lease_token"], attempt=data["attempt"])
    root = config.data_root / "renders" / data["job_id"]
    result = json.loads((root / "output/render-result.json").read_text(encoding="utf-8"))
    content = json.loads((root / "input/content.json").read_text(encoding="utf-8"))
    validate_content(content)
    video = (root / "output/did-you-know.mp4").read_bytes()
    digest_guard(video, result, content, env)
    digest_guard((directory / "did-you-know.mp4").read_bytes(), result, content, env)
    if (
        result != data["render"]
        or result.get("font_sha256")
        != tomllib.loads((ROOT / "config/snap/did_you_know.toml").read_text(encoding="utf-8"))[
            "slice"
        ]["profile"]["font_sha256"]
    ):
        raise ValueError("Render profile changed")
    if (
        content["expires_at"] <= time.time()
        or not all(result.get("network_isolation", {}).values())
        or set(result.get("network_isolation", {})) != {"ipv4", "ipv6", "udp", "egress_unreachable"}
    ):
        raise ValueError("Isolation/content invalid")
    if (
        result.get("safe_area", {}).get("result") != "PASS"
        or result.get("qa", {}).get("result") != "PASS"
        or result.get("glyph_coverage") != "PASS"
    ):
        raise ValueError("Quality missing")
    with repo.connect() as db:
        if quality_chain(db, data["job_id"], data["attempt"], result) != data["quality"]:
            raise ValueError("Owned quality chain changed")
    return config, repo, data, result


def _send_bundle(directory: str | Path, env: Mapping[str, str]) -> dict[str, Any]:
    # Workflow uploaded a persistent send-intent artifact before this command.
    # A timeout/cancel leaves it, so subsequent runs fail before Telegram.
    approval_guard(env)
    intent_id = env.get("SEND_INTENT_ARTIFACT_ID", "")
    if not re.fullmatch("[1-9][0-9]*", intent_id):
        raise ValueError("Durable intent ID missing")
    intent_guard(env, github_get("actions/artifacts/" + intent_id, env))
    config, repo, data, result = verify_bundle(directory, env)
    from .final_agent import evaluate_final, record_human_frame
    from .telegram_egress import deliver

    review = record_human_frame(
        repo, data["job_id"], data["lease_token"], data["attempt"], result, reviewer=OWNER
    )
    decision = evaluate_final(repo, data["job_id"], data["lease_token"], data["attempt"], result)
    if decision["delivery_decision"] != "APPROVED":
        raise ValueError("Final blocked")
    receipt = deliver(
        config,
        data["job_id"],
        env["VIDEO_SHA"],
        lease_token=data["lease_token"],
        review_mode="HUMAN_FRAME",
    )
    proof = {"human_review": review, "decision": decision, "receipt": receipt}
    (Path(directory) / "receipt.json").write_text(
        json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return proof


def send_bundle(directory: str | Path, env: Mapping[str, str]) -> dict[str, Any]:
    from .send_evidence import failure_receipt, network_started

    reset = network_started.set(False)
    try:
        return _send_bundle(directory, env)
    except Exception:
        # Fixed reason only: exception strings may contain secrets or URLs.
        failure_receipt(
            Path(directory) / "receipt.json",
            run_id=env.get("GITHUB_RUN_ID", ""),
            video_sha=env.get("VIDEO_SHA", ""),
            intent_id=env.get("SEND_INTENT_ARTIFACT_ID"),
            content_hash=env.get("CONTENT_HASH"),
            reason="SEND_REJECTED_OR_UNKNOWN",
        )
        raise
    finally:
        network_started.reset(reset)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["render", "preflight", "intent", "send"])
    parser.add_argument("--output", type=Path, default=Path("outputs/manual-preview"))
    args = parser.parse_args()
    env = dict(os.environ)
    if args.action == "render":
        data = render_bundle(
            args.output, {"run_id": env["GITHUB_RUN_ID"], "head_sha": env["GITHUB_SHA"]}
        )
        print(
            json.dumps({key: data["render"][key] for key in ("content_hash", "video_sha256")})
            + "; elapsed="
            + str(data["elapsed_seconds"])
        )
    elif args.action == "preflight":
        preflight(env)
    elif args.action == "intent":
        verify_bundle(args.output, env)
        target = Path("outputs/send-intent")
        new_output_directory(target)
        (target / "intent.json").write_text(
            json.dumps(
                {
                    "video_sha": env["VIDEO_SHA"],
                    "content_hash": env["CONTENT_HASH"],
                    "run_id": env["GITHUB_RUN_ID"],
                    "state": "EXTERNAL_EFFECT_INTENT",
                    "actor": env["GITHUB_ACTOR"],
                }
            )
        )
    else:
        try:
            send_bundle(args.output, env)
            print("Test video ACK recorded")
        except Exception:
            # Never log exception URLs, API body, tokens or artifact fencing values.
            print(
                "TEST_SEND_BLOCKED_OR_UNKNOWN; inspect durable intent and receipt", file=sys.stderr
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
