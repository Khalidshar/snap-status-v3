"""GitHub Actions HUMAN_FRAME test send for a reviewed Status weather bundle.

Mirrors Snap V2's manual path: render artifact -> owner review -> explicit
HUMAN_FRAME with exact digests -> durable intent -> one test-bot delivery.
No scheduler; the render job never sees Telegram secrets.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import platform_policy
from .send_diagnostics import attach_error, operation, payload, record_error, stage

if TYPE_CHECKING:
    from studio_core.config import Config

ROOT = Path(__file__).resolve().parents[3]
REPOSITORY = "Khalidshar/status-bot-v2"
OWNER = "Khalidshar"
RENDER_WORKFLOW = ".github/workflows/status-weather-manual.yml"
INTENT_PREFIX = "status-weather-send-attempt-"
BUNDLE_SECONDS = 3600
ISOLATION = {"ipv4", "ipv6", "udp", "egress_unreachable"}


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
    created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).timestamp()
    if not 0 <= now - created < BUNDLE_SECONDS:
        raise ValueError("Source expired")


def history_guard(artifacts: Any, video_sha: str) -> None:
    # Any earlier intent, even expired or with unknown outcome, blocks a resend.
    if any(item.get("name") == INTENT_PREFIX + video_sha for item in artifacts):
        raise ValueError("Prior send intent exists; unknown outcomes never retry")


def intent_guard(env: Mapping[str, str], artifact: Any) -> None:
    if not re.fullmatch("[1-9][0-9]*", env.get("SEND_INTENT_ARTIFACT_ID", "")):
        raise ValueError("Durable intent ID missing")
    if (
        str(artifact.get("id")) != env["SEND_INTENT_ARTIFACT_ID"]
        or artifact.get("name") != INTENT_PREFIX + env["VIDEO_SHA"]
        or artifact.get("expired") is not False
        or str(artifact.get("workflow_run", {}).get("id")) != env["GITHUB_RUN_ID"]
    ):
        raise ValueError("Durable intent binding invalid")


def github_get(path: str, env: Mapping[str, str]) -> Any:
    """Authenticated GitHub REST JSON read; never follows redirects to storage."""
    from urllib.request import HTTPRedirectHandler, Request, build_opener

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

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
    with build_opener(NoRedirect).open(request, timeout=30) as response:
        raw = response.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("GitHub response too large")
    return json.loads(raw)


def preflight(
    env: Mapping[str, str], get: Callable[[str, Mapping[str, str]], Any] = github_get
) -> None:
    platform_policy.reject_status_telegram()
    approval_guard(env)
    source_guard(get("actions/runs/" + env["SOURCE_RUN"], env), env)
    current = get("actions/runs/" + env["GITHUB_RUN_ID"], env)
    if (
        current.get("event") != "workflow_dispatch"
        or current.get("actor", {}).get("login", "").lower() != OWNER.lower()
        or current.get("triggering_actor", {}).get("login", "").lower() != OWNER.lower()
    ):
        raise ValueError("Authenticated Khalid dispatch missing")
    for page in range(1, 101):
        items = get("actions/artifacts?per_page=100&page=" + str(page), env)["artifacts"]
        history_guard(items, env["VIDEO_SHA"])
        if len(items) < 100:
            return
    raise ValueError("History scan incomplete")


def digest_guard(video: bytes, result: Any, content: Any, env: Mapping[str, str]) -> None:
    if (
        not 0 < len(video) <= 50_000_000
        or hashlib.sha256(video).hexdigest() != env["VIDEO_SHA"]
        or result.get("video_sha256") != env["VIDEO_SHA"]
    ):
        raise ValueError("Approved video differs")
    if (
        content.get("content_hash") != env["CONTENT_HASH"]
        or result.get("content_hash") != env["CONTENT_HASH"]
    ):
        raise ValueError("Approved content differs")


def bundle_config(directory: str | Path) -> "Config":
    from studio_core.config import Config

    config = Config.load(ROOT / "config/status/weather.toml")
    return replace(
        config,
        data_root=Path(directory) / "state",
        lease_seconds=BUNDLE_SECONDS,
        budgets={**config.budgets, "NORMAL": BUNDLE_SECONDS},
    )


def verify_bundle(
    directory: str | Path, env: Mapping[str, str], now: float | None = None
) -> tuple["Config", Any]:
    approval_guard(env)
    from studio_core.jobs import Repository
    from studio_core.storage import Storage

    from .weather import content_hash

    now = time.time() if now is None else now
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
        raise ValueError("Bundle was not an unapproved preview")
    if not re.fullmatch("[0-9a-f]{32}", str(data.get("job_id"))):
        raise ValueError("Bundle job invalid")
    if not now < data["expires_at"]:
        raise ValueError("Bundle expired")
    if (
        hashlib.sha256((directory / "preview.png").read_bytes()).hexdigest()
        != data["preview_sha256"]
    ):
        raise ValueError("Preview changed")
    config = bundle_config(directory)
    repo = Repository(config)
    job = repo.assert_owner(data["job_id"], data["lease_token"])
    if job["state"] != "VALIDATING":
        raise ValueError("Job is not awaiting review")
    root = Storage(config).workspace(data["job_id"])
    result = json.loads((root / "output/render-result.json").read_text(encoding="utf-8"))
    content = json.loads((root / "input/content.json").read_text(encoding="utf-8"))
    if content_hash(content) != content.get("content_hash"):
        raise ValueError("Content hash invalid")
    digest_guard((root / "output/weather.mp4").read_bytes(), result, content, env)
    digest_guard((directory / "weather.mp4").read_bytes(), result, content, env)
    if result != data["render"]:
        raise ValueError("Render result changed")
    isolation = result.get("network_isolation", {})
    if set(isolation) != ISOLATION or not all(isolation.values()):
        raise ValueError("Isolation proof missing")
    if result.get("qa", {}).get("result") != "PASS" or result.get("glyph_coverage") != "PASS":
        raise ValueError("Quality missing")
    return config, data


def failure_receipt(path: str | Path, env: Mapping[str, str]) -> Any:
    from .send_diagnostics import failure_receipt as write_failure

    return write_failure(path, env)


def send_bundle(
    directory: str | Path,
    env: Mapping[str, str],
    get: Callable[[str, Mapping[str, str]], Any] = github_get,
    transport: Any = None,
) -> dict[str, Any]:
    platform_policy.reject_status_telegram()
    from .telegram_egress import api, atomic_json, deliver

    with operation() as diagnostic:
        try:
            stage("APPROVAL")
            approval_guard(env)
            stage("DURABLE_INTENT")
            intent_guard(
                env, get("actions/artifacts/" + env.get("SEND_INTENT_ARTIFACT_ID", ""), env)
            )
            stage("BUNDLE_VALIDATION")
            config, data = verify_bundle(directory, env)
            receipt = deliver(
                config,
                data["job_id"],
                env["VIDEO_SHA"],
                transport or api,
                lease_token=data["lease_token"],
                review_mode="HUMAN_FRAME",
            )
            proof = {"human_review": {"mode": "HUMAN_FRAME", "reviewer": OWNER}, "receipt": receipt}
            stage("RECEIPT_PERSISTENCE")
            atomic_json(Path(directory) / "receipt.json", proof)
            return proof
        except Exception as error:
            record_error(error)
            try:
                failure_receipt(Path(directory) / "receipt.json", env)
            except Exception:
                diagnostic.evidence_write_failed = True
            attach_error(error, env)
            raise


def write_intent(env: Mapping[str, str], target: Path = Path("outputs/send-intent")) -> None:
    platform_policy.reject_status_telegram()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(mode=0o750)
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


def retain_workflow_diagnostic(
    value: object, env: Mapping[str, str], target: str | Path = Path("outputs/send-intent")
) -> bool:
    """Copy safe diagnostics into the already-retained results directory.

    Never create or alter an intent, and never use this copy to authorize a send.
    The original intent artifact was uploaded before delivery and is unchanged.
    """
    from .telegram_egress import atomic_json

    target = Path(target)
    intent_path = target / "intent.json"
    diagnostic_path = target / "send-diagnostics.json"
    if (
        not target.is_dir()
        or target.is_symlink()
        or intent_path.is_symlink()
        or diagnostic_path.is_symlink()
        or not intent_path.is_file()
    ):
        return False
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    expected = {
        "video_sha": env.get("VIDEO_SHA"),
        "content_hash": env.get("CONTENT_HASH"),
        "run_id": env.get("GITHUB_RUN_ID"),
        "state": "EXTERNAL_EFFECT_INTENT",
        "actor": OWNER,
    }
    if not isinstance(intent, dict) or any(intent.get(key) != expected[key] for key in expected):
        return False
    atomic_json(diagnostic_path, value)
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["preflight", "intent", "send"])
    parser.add_argument("--bundle", type=Path, default=Path("outputs/status-manual"))
    args = parser.parse_args()
    env = dict(os.environ)
    if args.action == "preflight":
        preflight(env)
    elif args.action == "intent":
        verify_bundle(args.bundle, env)
        write_intent(env)
    else:
        with operation() as diagnostic:
            try:
                send_bundle(args.bundle, env)
                print("Status test video ACK recorded")
            except Exception as error:
                record_error(error)
                value = payload(env, diagnostic)
                try:
                    retain_workflow_diagnostic(value, env)
                except Exception:
                    diagnostic.evidence_write_failed = True
                    value = payload(env, diagnostic)
                # Only our operation context is trusted, never exception attributes.
                print(json.dumps(value, sort_keys=True), file=sys.stderr)
                print(
                    "STATUS_TEST_SEND_BLOCKED_OR_UNKNOWN; inspect durable intent and receipt",
                    file=sys.stderr,
                )
                raise SystemExit(1)


if __name__ == "__main__":
    main()
