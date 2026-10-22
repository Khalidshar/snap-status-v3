"""Status V2 weather slice. Owned computation, no scheduler or external delivery."""

import hashlib
import json
import os
import re
import stat
import time
from typing import Any

from studio_core.config import Config
from studio_core.jobs import Repository
from studio_core.jobs.component_lock import ComponentLocks
from studio_core.jobs.gateway import CommandGateway
from studio_core.observability.agent_runs import digest, run_agent
from studio_core.storage import Storage

from .agents import arabic_output, contract, source_output, source_request, weather_payload
from .weather import content_hash, fetch_weather


def prepare(
    config: Config,
    settings: dict[str, Any],
    client: Any,
    secret: str,
    event_id: str,
    *,
    synthetic: bool = False,
) -> dict[str, Any]:
    if config.app != "status-bot-v2" or type(synthetic) is not bool:
        raise ValueError("STATUS_SCOPE_INVALID")
    repo = Repository(config)
    gateway = CommandGateway(config, repo, secret)
    actor = config.owners["scheduler"][0]
    now = time.time()
    body = {
        "channel": "scheduler",
        "actor": actor,
        "event_id": event_id,
        "command": "weather",
        "priority": "NORMAL",
    }
    job, created = gateway.accept(body, now, gateway.signature(body, now))
    if not created:
        raise ValueError("Explicit duplicate job; no automatic render replay")
    # Never claim or mutate a different queued job while preparing this request.
    claim = repo.claim(job["id"])
    if not claim or claim["id"] != job["id"]:
        raise RuntimeError("Queue busy")
    token, attempt = claim["lease_token"], claim["attempt"]
    locks = ComponentLocks(repo)
    lease = None
    try:
        lease = locks.acquire("weather-input", "USE", min(1200, config.lease_seconds))
        root = Storage(config).workspace(job["id"])
        repo.transition(job["id"], token, "RESEARCHING")

        def fetch() -> dict[str, Any]:
            content, failures = fetch_weather(client, settings, now)
            if synthetic:
                content.update(
                    title="تجربة الطقس",
                    source="synthetic-fixture",
                    source_url="fixture:weather",
                    attribution="SYNTHETIC TEST / NOT LIVE WEATHER",
                )
                content["content_hash"] = content_hash(content)
            return {"content": content, "failures": failures}

        sourced = run_agent(
            repo,
            job["id"],
            token,
            attempt,
            contract("fact_source"),
            {"command": "weather", "synthetic": synthetic},
            fetch,
            source_request,
            source_output,
        )
        repo.transition(
            job["id"],
            token,
            "PROCESSING",
            {"source": sourced["content"]["source"], "fallbacks": sourced["failures"]},
        )
        checked = run_agent(
            repo,
            job["id"],
            token,
            attempt,
            contract("arabic"),
            sourced,
            lambda: sourced,
            weather_payload,
            arabic_output,
        )
        locks.assert_holder(lease)
        # Bind the filesystem commit to the exact current owner and attempt.
        with repo.owned_transaction(job["id"], token, attempt=attempt):
            fd = os.open(
                root / "input/content.json",
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(checked["content"], stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
        for state in ("DOWNLOADING", "DESIGNING", "RENDERING"):
            repo.transition(job["id"], token, state)
        return {
            "claim": claim,
            "content": checked["content"],
            "source_payload": checked,
            "synthetic_source": synthetic,
            "external_delivery": False,
        }
    except Exception:
        try:
            repo.transition(job["id"], token, "FAILED", {"code": "STATUS_WEATHER_PREPARE_FAILED"})
        except Exception:
            pass
        raise
    finally:
        if lease is not None:
            locks.release(lease)


def record_validation(
    config: Config, prepared: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    if config.app != "status-bot-v2":
        raise ValueError("STATUS_SCOPE_INVALID")
    locks = ComponentLocks(Repository(config))
    with locks.hold("weather-render", "USE", 25):
        return _record_validation(config, prepared, result)


def _record_validation(
    config: Config, prepared: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    if config.app != "status-bot-v2":
        raise ValueError("STATUS_SCOPE_INVALID")
    claim = prepared["claim"]
    repo = Repository(config)
    job, token, attempt = claim["id"], claim["lease_token"], claim["attempt"]
    repo.assert_owner(job, token, attempt=attempt)
    root = Storage(config).workspace(job)
    payload = {"source_payload": prepared["source_payload"], "render": result}

    def valid_input(value: Any) -> bool:
        source, render = value["source_payload"], value["render"]
        if (
            not weather_payload(source)
            or digest(source["content"]) != digest(prepared["content"])
            or render.get("job_id") != job
            or render.get("content_hash") != source["content"]["content_hash"]
            or render.get("qa", {}).get("result") != "PASS"
            or not isinstance(render.get("video_sha256"), str)
            or not re.fullmatch("[0-9a-f]{64}", render["video_sha256"])
        ):
            return False
        # The quality input contains source + render, so validate lineage
        # explicitly instead of comparing its combined hash to the source hash.
        with repo.connect() as db:
            prior = db.execute(
                "SELECT output_sha FROM agent_runs WHERE app=? AND job_id=? AND attempt=? AND role='arabic' AND status='SUCCEEDED'",
                (config.app, job, attempt),
            ).fetchone()
        if not prior or prior["output_sha"] != digest(source):
            return False
        video = root / "output/weather.mp4"
        if video.is_symlink() or not video.is_file() or not 0 < video.stat().st_size <= 50_000_000:
            return False
        descriptor = os.open(video, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 50_000_000:
                return False
            if hashlib.file_digest(stream, "sha256").hexdigest() != render["video_sha256"]:
                return False
            after = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                return False
        rendered = root / "output/render-result.json"
        if (
            rendered.is_symlink()
            or not rendered.is_file()
            or not 0 < rendered.stat().st_size <= 1024 * 1024
        ):
            return False
        descriptor = os.open(rendered, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            recorded = stream.read(1024 * 1024 + 1)
        if len(recorded) > 1024 * 1024 or digest(json.loads(recorded)) != digest(render):
            return False
        stored = root / "input/content.json"
        if stored.is_symlink() or digest(json.loads(stored.read_bytes())) != digest(
            source["content"]
        ):
            return False
        return True

    def accept() -> dict[str, Any]:
        return {
            "job_id": job,
            "attempt": attempt,
            "content_hash": result["content_hash"],
            "video_sha256": result["video_sha256"],
            "qa": "PASS",
            "visual_review": "PENDING",
        }

    checked = run_agent(
        repo,
        job,
        token,
        attempt,
        contract("quality"),
        payload,
        accept,
        valid_input,
        lambda value, original: value == accept(),
        bind_dependency=False,
    )
    with repo.owned_transaction(job, token, attempt=attempt) as db:
        repo.transition(job, token, "VALIDATING", {"qa": checked["qa"]}, db=db)
        value = {
            "app": config.app,
            "job_id": job,
            "state": "VALIDATING",
            "render": result,
            "content": prepared["content"],
            "source_payload": prepared["source_payload"],
            "synthetic_source": prepared["synthetic_source"],
            "external_delivery": False,
            "delivery": "NOT_ATTEMPTED",
            "publication": "NOT_IMPLEMENTED",
            "destination": "whatsapp",
            "visual_review": "PENDING",
        }
        Storage(config).manifest(job, value)
    return value
