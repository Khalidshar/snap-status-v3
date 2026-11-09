"""Owned WhatsApp content commands using the unchanged shared Snap renderer and checks."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from studio_core.config import Config
from studio_core.jobs import Repository
from studio_core.jobs.component_lock import ComponentLocks
from studio_core.network.client import NetworkClient
from studio_core.network.video import VideoPolicy
from studio_core.observability import role_status
from studio_core.observability.agent_runs import AgentContract, digest, run_agent
from studio_core.observability.failure_code import failure_code
from studio_core.presentation import command_cards, footage
from studio_core.presentation.arabic_agent import check_content
from studio_core.presentation.content_agents import CHECKS, run_check
from studio_core.presentation.did_you_know import (
    WINDOW_SECONDS,
    choose_fact,
    seed_history,
    validate_content,
)
from studio_core.presentation.media_checks import check_media, check_voice
from studio_core.religious.quran_pipeline import render as shared_render
from studio_core.religious.quran_pipeline import stage as stage_quran
from studio_core.renderer import write_once
from studio_core.sources import event_video
from studio_core.sources.news import validate as validate_news

from .server_preview import STATE, safe_path
from .whatsapp_contract import DeliveryBlocked, identifier, require, response_id
from .whatsapp_outbox import MAX_VIDEO, read_bound

ROOT = STATE / "content-clips"
KINDS = {
    "facts": "did_you_know",
    "tech": "tech",
    "style": "style",
    "funny": "funny",
    "night": "night",
    "hadith": "hadith",
    "dua": "dua",
    "fast": "fast",
    "verse": "verse",
    "adhkar_am": "adhkar_am",
    "adhkar_pm": "adhkar_pm",
    "friday": "friday",
    "kahf": "kahf",
    "riddle": "riddle",
    "isha": "verse",
    "history": "history",
}
CHAIN = (
    "fact_source",
    "arabic",
    "memory",
    "rotation",
    "proofreading",
    "translation",
    "design",
    "voice",
    "media_verification",
    "judge",
    "quality",
    "final",
)
PLAN = digest(
    {
        "app": "status-bot-v2",
        "version": "1.0.0",
        "chain": CHAIN,
        "renderer": "snap-shared-verbatim-v1",
    }
)


def initialize(repo: Any) -> None:
    with repo.transaction() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS command_videos(tag TEXT PRIMARY KEY, binding TEXT NOT NULL, kind TEXT NOT NULL, content_json TEXT, state TEXT NOT NULL, at REAL NOT NULL, acked_at REAL, video_sha TEXT, message_tag TEXT, code TEXT)"
        )


def window(outbox: Any) -> None:
    with outbox.repo.connect() as db:
        row = db.execute(
            "SELECT last_inbound_at FROM whatsapp_windows WHERE identity_binding=?",
            (outbox.identity.binding,),
        ).fetchone()
    require(
        row is not None and 0 <= outbox.repo.clock() - row[0] < 86400, "WA_SERVICE_WINDOW_CLOSED"
    )


def valid(value: Any, news: bool) -> bool:
    if isinstance(value, dict) and "faith" in value and "dates" in value:
        from studio_core.sources.daily_brief import validate_brief

        validate_brief(value, time.time())
    elif news:
        validate_news(value, time.time())
    else:
        validate_content(value)
    return True


def render_event(tag: str, root: Path = ROOT) -> None:
    if not re.fullmatch("[0-9a-f]{64}", tag):
        raise ValueError("Invalid content event")
    directory = safe_path(root / tag)
    request = json.loads(read_bound(directory, "request.json", 65536))
    require(request.get("app") == "status-bot-v2", "WA_APP_SCOPE_INVALID")
    require(
        request["settings"]["spool_root"] == str(directory / "spool/renders")
        and request["settings"]["input_root"] == str(directory / "input/renders"),
        "WA_RENDER_ROOT_INVALID",
    )
    content, settings = request["content"], request["settings"]
    valid(content, request["news"])
    # The offline worker never sees the WhatsApp identity, token or signing keys.
    if request.get("brief"):
        from studio_core.presentation.daily_brief import render

        result = render(
            content, directory / "brief", app="status-bot-v2", media=directory / "brief-media"
        )
        result["video_path"] = "brief/video.mp4"
    elif request["news"]:
        parts = command_cards.lines(content["headline"])
        if len(parts) > 9:
            raise ValueError("Headline exceeds card")
        original = directory / "media/original.json"
        if original.exists():
            result = command_cards.original(content, original, directory / "news")
        else:
            result = command_cards.render(
                [
                    (content["title"], 52, 430),
                    *[(s, 46, 600 + i * 75) for i, s in enumerate(parts)],
                    ("المصدر: " + content["source"], 34, 1340),
                    (content["published_label"], 26, 1430),
                ],
                directory / "news",
                backdrop=footage.pinned(
                    {**content, "body": content["headline"]},
                    directory / "media/background/footage.json",
                ),
            )
        result["content_hash"] = content["content_hash"]
        result["video_path"] = "news/video.mp4"
    else:
        result = shared_render(
            type("Scope", (), {"app": "status-bot-v2"})(),
            settings,
            request["job"],
            content["content_hash"],
            1,
        )
        result["video_path"] = str(
            (Path(settings["spool_root"]).parent / result["artifact"]).relative_to(directory)
        )
    write_once(directory / "result.json", json.dumps(result, ensure_ascii=False).encode())


def build(
    outbox: Any,
    item: dict[str, Any],
    key: str,
    *,
    source: Any = None,
    root: Path = ROOT,
    start: Any = None,
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    require(identifier(item.get("tag"), "[0-9a-f]{64}"), "WA_COMMAND_INVALID")
    from studio_core.production import enabled as production_enabled
    from studio_core.production.model import TITLES as PRODUCTION_KINDS

    production = production_enabled()
    brief = key == "brief" and not production
    news = source is not None and not brief and not production
    require(
        (production and key in PRODUCTION_KINDS)
        or brief
        or ((source.get("command") == key) if news else key in KINDS),
        "WA_COMMAND_KIND_INVALID",
    )
    command = "brief" if brief else "news" if news else "did_you_know"
    directory = safe_path(root / item["tag"])
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    config = replace(
        outbox.repo.config,
        data_root=directory / "state",
        command_allowlist=(command,),
        lease_seconds=1200,
    )
    repo = Repository(config, clock=outbox.repo.clock)
    job, _ = repo.enqueue(
        item["tag"], "owner-command", command, "NORMAL", "input", repo.clock() + 1200
    )
    claim = repo.claim(job_id=job["id"])
    if claim is None:
        raise ValueError("Content job not claimable")
    args = claim["id"], claim["lease_token"], claim["attempt"]
    repo.transition(args[0], args[1], "RESEARCHING")
    if not news and not brief:
        with outbox.repo.connect() as db:
            rows = db.execute(
                "SELECT content_json,state,at,acked_at FROM command_videos WHERE tag<>? AND binding=? AND content_json IS NOT NULL",
                (item["tag"], outbox.identity.binding),
            ).fetchall()
        entries = []
        for row in rows:
            prior = json.loads(row["content_json"])
            if prior.get("type") != "did_you_know" or prior.get("legacy_production"):
                continue
            at = row["acked_at"] or row["at"]
            until = at + WINDOW_SECONDS if row["state"] == "API_ACCEPTED" else 253402300799.0
            if until > repo.clock():
                entries.append(
                    {
                        "kind": prior.get("kind", "did_you_know"),
                        "category": prior["category"],
                        "fact_id": prior["fact_id"],
                        "body_hash": hashlib.sha256(prior["body"].encode()).hexdigest(),
                        "at": at,
                        "until": until,
                    }
                )
        with repo.transaction() as db:
            seed_history(db, entries)
    previous = None

    def stage(role: str, payload: Any, callback: Any, output: Any, *, bind: bool = True) -> Any:
        nonlocal previous
        contract = AgentContract(
            "status-bot-v2",
            command,
            role,
            "1.0.0",
            PLAN,
            (previous,) if previous else (),
            900 if role == "design" or (production and role == "fact_source") else 30,
        )
        ledger = outbox.repo.config.data_root / "agent-status.sqlite"
        role_status.record(ledger, "status-bot-v2", role, "RUNNING")
        try:
            result = run_agent(
                repo,
                *args,
                contract,
                payload,
                callback,
                lambda v: isinstance(v, dict),
                lambda value, payload: output(value),
                bind_dependency=bind,
            )
        except Exception as error:
            print(f"STATUS_CLIP_FAILURE stage={role} code={failure_code(error)}", file=sys.stderr)
            role_status.record(ledger, "status-bot-v2", role, "FAILED")
            raise
        role_status.record(ledger, "status-bot-v2", role, "SUCCEEDED")
        previous = role
        return result

    def check_brief(value: dict[str, Any]) -> dict[str, Any]:
        valid(value, False)
        for part in ("saudi", "tech"):
            check_news("proofreading", value[part], expected=part)
            check_news("translation", value[part], expected=part)
        return value

    def check_news(role: str, value: dict[str, Any], expected: str | None = None) -> dict[str, Any]:
        valid(value, True)
        require(value["command"] == (expected or key), "WA_NEWS_ROTATION_MISMATCH")
        if role == "memory":
            with outbox.repo.connect() as db:
                prior = db.execute(
                    "SELECT content_json FROM command_videos WHERE tag<>? AND binding=? AND at>? AND content_json IS NOT NULL",
                    (item["tag"], outbox.identity.binding, outbox.repo.clock() - 30 * 86400),
                ).fetchall()
            require(
                not any(json.loads(r[0]).get("fingerprint") == value["fingerprint"] for r in prior),
                "WA_NEWS_REPEAT",
            )
        elif role == "proofreading":
            from .agents import proofread

            proofread(value["headline"])
        elif role in {"arabic", "translation"}:
            from studio_core.presentation.arabic_agent import check_text

            for part in command_cards.lines(value["headline"], width=100):
                check_text(part.strip())
        return value

    content = stage(
        "fact_source",
        {"kind": key},
        lambda: (
            source
            if (news or brief)
            else choose_fact(repo, *args, kind=key if production else KINDS[key])[0]
        ),
        lambda v: valid(v, news),
    )
    content = stage(
        "arabic",
        content,
        lambda: (
            check_brief(content)
            if brief
            else check_news("arabic", content)
            if news
            else check_content(content)
        ),
        lambda v: valid(v, news),
    )
    for role in CHECKS:
        current = content
        content = stage(
            role,
            content,
            lambda role=role, value=current: (
                check_brief(value)
                if brief
                else check_news(role, value)
                if news
                else run_check(role, repo, *args, value)
            ),
            lambda v, expected=current: v == expected and valid(v, news),
        )
    with outbox.repo.transaction() as db:
        db.execute(
            "UPDATE command_videos SET content_json=? WHERE tag=?",
            (json.dumps(content, ensure_ascii=False), item["tag"]),
        )
    for phase in ("PROCESSING", "DOWNLOADING", "DESIGNING", "RENDERING"):
        repo.transition(args[0], args[1], phase)
    settings: dict[str, Any] = {
        "spool_root": str(directory / "spool/renders"),
        "input_root": str(directory / "input/renders"),
        "worker_uid": os.getuid(),
        "profile": command_cards.profile(),
        "egress_socket": "/run/madar-status-content-no-egress.sock",
    }
    source_path = Path(settings["input_root"]) / args[0] / "attempts/1/input/content.json"
    source_path.parent.mkdir(parents=True, mode=0o700)
    write_once(source_path, json.dumps(content, ensure_ascii=False).encode())
    os.chmod(source_path, 0o640)
    socket = Path("/run/madar/status-video.sock")
    if brief:
        for part in ("saudi", "tech"):
            footage.prepare(
                {**content[part], "body": content[part]["headline"]},
                directory / "brief-media" / part,
                socket,
            )
    elif news:
        policy = json.loads(
            (Path(__file__).resolve().parents[3] / "config/media/source-video.json").read_text()
        )
        client = NetworkClient(cast(Config, VideoPolicy(socket)))
        original = event_video.prepare(content, directory / "media", client, policy)
        if original is None:
            footage.prepare(
                {**content, "body": content["headline"]}, directory / "media/background", socket
            )
    elif not stage_quran(content, "status-bot-v2", args[0], source_path) and settings[
        "profile"
    ].get("footage"):
        footage.prepare(content, source_path.parent / "background", socket)
    write_once(
        directory / "request.json",
        json.dumps(
            {
                "app": "status-bot-v2",
                "job": args[0],
                "content": content,
                "settings": settings,
                "news": news,
                "brief": brief,
            },
            ensure_ascii=False,
        ).encode(),
    )

    def design() -> Any:
        unit = "madar-v3-status-content@" + item["tag"] + ".service"
        if start is None:
            subprocess.run(["/usr/bin/systemctl", "start", unit], check=True, timeout=900)  # noqa: S603
        else:
            start(unit)
        return json.loads(read_bound(directory, "result.json", 65536))

    result = stage(
        "design",
        content,
        design,
        lambda r: isinstance(r, dict) and r.get("content_hash") == content["content_hash"],
    )
    repo.transition(args[0], args[1], "VALIDATING")
    # Keep the shared renderer result byte-identical for its independent media agent.
    checked = {k: v for k, v in result.items() if k != "video_path"}

    def verify_voice() -> Any:
        if not news and not brief:
            check_voice(checked)
        else:
            require(
                result.get("audio_policy") in {"SILENT", "ORIGINAL_SOURCE", "BRIEF_MIX"},
                "WA_AUDIO_POLICY_CHANGED",
            )
        return result

    result = stage("voice", result, verify_voice, lambda r: r == result)

    def verify_media() -> Any:
        if brief:
            require(
                isinstance(result.get("duration"), (int, float)) and 30 <= result["duration"] <= 60,
                "WA_BRIEF_DURATION_INVALID",
            )
        if not news and not brief:
            check_media(checked, settings)
        video = read_bound(directory, result["video_path"], MAX_VIDEO)
        require(
            hashlib.sha256(video).hexdigest() == result.get("video_sha256"),
            "WA_CONTENT_VIDEO_CHANGED",
        )
        require(
            all(
                result.get("network_isolation", {}).get(k) is True
                for k in ("ipv4", "ipv6", "udp", "egress_unreachable")
            ),
            "WA_CONTENT_ISOLATION_MISSING",
        )
        require(result.get("safe_area", {}).get("result") == "PASS", "WA_CONTENT_SAFE_AREA_FAILED")
        valid(content, news)
        return result

    result = stage("media_verification", result, verify_media, lambda r: r == result)
    for role in ("judge", "quality", "final"):
        result = stage(role, result, verify_media, lambda r, expected=result: r == expected)
    write_once(
        directory / "controller.json", json.dumps({"job": args[0], "token": args[1]}).encode()
    )
    return content, result, read_bound(directory, result["video_path"], MAX_VIDEO)


def complete(outbox: Any, item: dict[str, Any], root: Path) -> None:
    directory = safe_path(root / item["tag"])
    control = json.loads(read_bound(directory, "controller.json", 4096))
    repo = Repository(
        replace(
            outbox.repo.config,
            data_root=directory / "state",
            command_allowlist=("did_you_know", "news", "brief"),
            lease_seconds=1200,
        ),
        clock=outbox.repo.clock,
    )
    repo.transition(control["job"], control["token"], "DELIVERING")
    repo.transition(control["job"], control["token"], "COMPLETED")


def close_failure(outbox: Any, tag: str, error: Exception) -> bool:
    """Return whether this attempt failed before any send intent was recorded."""
    code = failure_code(error)
    if isinstance(error, DeliveryBlocked) and identifier(str(error), r"WA_[A-Z0-9_]{1,80}"):
        code = str(error)
    with outbox.repo.transaction() as db:
        retryable = db.execute(
            "UPDATE command_videos SET state='BLOCKED',code=? WHERE tag=?"
            " AND state IN ('WORKING','UPLOAD_INTENT')",
            (code, tag),
        ).rowcount
        db.execute(
            "UPDATE command_videos SET state='BLOCKED_OR_UNKNOWN',code=? WHERE tag=?"
            " AND state='SEND_INTENT'",
            (code, tag),
        )
    return bool(retryable)


def deliver_built(
    outbox: Any,
    transport: Any,
    item: dict[str, Any],
    key: str,
    content: dict[str, Any],
    result: dict[str, Any],
    video: bytes,
    runtime: Any,
    *,
    root: Path,
    source: Any,
    locks: Any,
    lease: Any,
) -> None:
    """Upload and send one built clip once; the chat command and panel approval share it."""
    description = None
    if content.get("legacy_production"):
        from studio_core.production.caption import platform_caption

        description = platform_caption(content)
    require(not safe_path(STATE / "HALT").exists(), "WA_SEND_HALTED")
    window(outbox)
    locks.assert_holder(lease)
    with outbox.repo.transaction() as db:
        changed = db.execute(
            "UPDATE command_videos SET state='UPLOAD_INTENT',video_sha=? WHERE tag=?"
            " AND state='WORKING'",
            (result["video_sha256"], item["tag"]),
        ).rowcount
        require(changed == 1, "WA_COMMAND_ALREADY_CONSUMED")
    if result.get("quran_foundation"):
        from studio_core.religious.quran_pipeline import check_result

        check_result(result)
    media = response_id(transport.upload_video(video), "upload", outbox.identity.recipient)
    require(not safe_path(STATE / "HALT").exists(), "WA_SEND_HALTED")
    window(outbox)
    locks.assert_holder(lease)
    valid(content, source is not None)
    with outbox.repo.transaction() as db:
        db.execute("UPDATE command_videos SET state='SEND_INTENT' WHERE tag=?", (item["tag"],))
    if result.get("quran_foundation"):
        check_result(result)
    sent = (
        transport.send_video(media, caption=description)
        if description is not None
        else transport.send_video(media)
    )
    message = response_id(sent, "send", outbox.identity.recipient)
    with outbox.repo.transaction() as db:
        db.execute(
            "UPDATE command_videos SET state='API_ACCEPTED',acked_at=?,message_tag=? WHERE tag=?",
            (outbox.repo.clock(), outbox.identity.tag("message", message), item["tag"]),
        )
    complete(outbox, item, root)
    with runtime.locked():
        runtime.append(
            "ACK",
            video_sha=result["video_sha256"],
            content_kind=key,
            command_tag=item["tag"],
        )


def send(
    outbox: Any,
    transport: Any,
    item: dict[str, Any],
    key: str,
    runtime: Any,
    *,
    source: Any = None,
    root: Path = ROOT,
    start: Any = None,
) -> None:
    require(outbox.repo.config.app == "status-bot-v2", "WA_APP_SCOPE_INVALID")
    require(identifier(item.get("tag"), "[0-9a-f]{64}"), "WA_COMMAND_INVALID")
    require(
        transport.credentials.phone_number == outbox.identity.phone_number
        and transport.credentials.recipient == outbox.identity.recipient,
        "WA_TRANSPORT_IDENTITY_MISMATCH",
    )
    initialize(outbox.repo)
    require(not safe_path(STATE / "HALT").exists(), "WA_SEND_HALTED")
    window(outbox)
    locks = ComponentLocks(outbox.repo)
    lease = locks.acquire("command-clips", "USE", 1200)
    try:
        with outbox.repo.transaction() as db:
            require(
                not db.execute(
                    "SELECT 1 FROM command_videos WHERE tag=?", (item["tag"],)
                ).fetchone(),
                "WA_COMMAND_ALREADY_CONSUMED",
            )
            db.execute(
                "INSERT INTO command_videos(tag,binding,kind,state,at) VALUES(?,?,?,'WORKING',?)",
                (item["tag"], outbox.identity.binding, key, outbox.repo.clock()),
            )
        phase = "build"
        try:
            content, result, video = build(outbox, item, key, source=source, root=root, start=start)
            phase = "delivery"
            deliver_built(
                outbox,
                transport,
                item,
                key,
                content,
                result,
                video,
                runtime,
                root=root,
                source=source,
                locks=locks,
                lease=lease,
            )
        except Exception as error:
            print(f"STATUS_CLIP_FAILURE stage={phase} code={failure_code(error)}", file=sys.stderr)
            close_failure(outbox, item["tag"], error)
            raise
    finally:
        locks.release(lease)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("event")
    args = parser.parse_args()
    try:
        render_event(args.event)
    except Exception as error:
        print(
            f"STATUS_CLIP_FAILURE stage=render_worker code={failure_code(error)}", file=sys.stderr
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
