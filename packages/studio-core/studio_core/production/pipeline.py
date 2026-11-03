# ruff: noqa: E501
"""Adapters into the existing owned source/agent/render/adoption pipeline."""

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from studio_core.config import Config
from studio_core.jobs import Repository
from studio_core.renderer import file_sha256, write_once

from .model import canonical, resource, validate
from .prepare import prepare


def source_root(app: str, job: str) -> Path:
    short = "snap" if app == "snap-bot-v2" else "status"
    return (
        Path(os.getenv("MADAR_PRODUCTION_STATE_ROOT", f"/var/lib/madar/{short}/production"))
        / "payloads"
        / job
    )


def choose(repo: Repository, job: str, token: str, attempt: int, key: str) -> dict[str, Any]:
    with repo.owned_transaction(job, token, attempt=attempt) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS fact_reservations(job_id TEXT PRIMARY KEY REFERENCES jobs(id),fact_id TEXT NOT NULL,body_hash TEXT NOT NULL,reserved_at REAL NOT NULL,content_json TEXT NOT NULL)"
        )
        prior = db.execute(
            "SELECT content_json FROM fact_reservations WHERE job_id=?", (job,)
        ).fetchone()
        if prior:
            value = json.loads(prior["content_json"])
            validate(value)
            return dict(value)
    value = prepare(repo.config.app, key, job, source_root(repo.config.app, job))
    from .oversight import execute

    execute(repo, job, token, attempt, value, source_root(repo.config.app, job))
    with repo.owned_transaction(job, token, attempt=attempt) as db:
        db.execute(
            "INSERT INTO fact_reservations VALUES(?,?,?,?,?)",
            (
                job,
                value["fact_id"],
                hashlib.sha256(value["body"].encode()).hexdigest(),
                repo.clock(),
                canonical(value).decode(),
            ),
        )
        repo.event(
            db, job, "legacy.source.reserved", {"kind": key, "content_hash": value["content_hash"]}
        )
    return value


def stage(content: dict[str, Any], app: str, job: str, input_path: Path) -> None:
    source = source_root(app, job)
    p = validate(content, assets=source)
    if p["app"] != app or json.loads((source / "content.json").read_bytes()) != content:
        raise ValueError("LEGACY_STAGE_SCOPE_CHANGED")
    assets = input_path.parent / "assets"
    assets.mkdir(mode=0o750)
    for a in p["assets"]:
        path = resource(source, a["file"], a["sha256"])
        write_once(input_path.parent / a["file"], path.read_bytes())
        track_qf(p, input_path.parent / a["file"], asset=a)
    write_once(
        input_path.with_name("legacy.json"),
        canonical({"schema": 1, "app": app, "job": job, "content_hash": content["content_hash"]}),
    )


def render(
    config: Config, settings: dict[str, Any], job: str, digest: str, attempt: int
) -> dict[str, Any]:
    from .render import render as render_scenes

    source = Path(settings["input_root"]) / job / "attempts" / str(attempt) / "input"
    if any(p.is_symlink() for p in (source, *source.parents)):
        raise ValueError("LEGACY_RENDER_UNSAFE")
    value = json.loads((source / "content.json").read_bytes())
    p = validate(value, assets=source)
    marker = json.loads((source / "legacy.json").read_bytes())
    if (
        p["app"],
        marker.get("app"),
        marker.get("job"),
        marker.get("content_hash"),
        value["content_hash"],
    ) != (config.app, config.app, job, digest, digest) or value["expires_at"] <= time.time():
        raise ValueError("LEGACY_RENDER_SCOPE_CHANGED")
    work = Path(settings["spool_root"]) / job / "attempts" / str(attempt)
    built = render_scenes(source, work / "production")
    out = work / "output"
    out.mkdir(mode=0o750, exist_ok=True)
    for old, new in [("video.mp4", "did-you-know.mp4"), ("shot-3.jpg", "preview-source.jpg")]:
        shutil.copyfile(work / "production" / old, out / new)
    from studio_core.renderer import run

    run(
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(out / "preview-source.jpg"),
            str(out / "preview.png"),
        ],
        30,
    )
    result = {
        "job_id": job,
        "attempt": attempt,
        "content_hash": digest,
        "content_type": "did_you_know",
        "artifact": f"renders/{job}/attempts/{attempt}/output/did-you-know.mp4",
        "video_sha256": file_sha256(out / "did-you-know.mp4"),
        "font_sha256": settings["profile"]["font_sha256"],
        "latin_font_sha256": settings["profile"]["latin_font_sha256"],
        "tool_version": run(["/usr/bin/ffmpeg", "-version"], 5).stdout.splitlines()[0],
        "glyph_coverage": "PASS",
        "network_isolation": built["network_isolation"],
        "safe_area": {"result": "PASS", "boxes": [built["brand"]["clip_box"]]},
        "qa": built["qa"],
        "legacy_production": {
            "content": value,
            "brand": built["brand"],
            "rebuild_attempt": built["attempt"],
        },
    }
    write_once(
        out / "render-result.json",
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2).encode(),
    )
    for path in (
        out / "did-you-know.mp4",
        work / "production/video.mp4",
        *sorted((work / "production").glob("attempt-*/*.mp4")),
        *sorted((work / "production").glob("attempt-*/mix.wav")),
    ):
        track_qf(p, path)
    return result


def track_qf(plan: dict[str, Any], path: Path, *, asset: dict[str, Any] | None = None) -> None:
    records = (
        [a for a in plan["assets"] if a.get("qf_job")]
        if asset is None
        else [asset]
        if asset.get("qf_job")
        else []
    )
    if not records:
        return
    from studio_core.religious.quran_pipeline import store_for

    store = store_for(plan["app"])
    store.sweep()
    record = min(records, key=lambda a: a["expires_at"])
    store.track(path, record["qf_job"], record["expires_at"], file_sha256(path))


def check_result(result: dict[str, Any]) -> float:
    record = result["legacy_production"]
    content = record["content"]
    p = validate(content)
    if result["content_hash"] != content["content_hash"] or content["expires_at"] <= time.time():
        raise ValueError("LEGACY_RESULT_CHANGED_OR_EXPIRED")
    human = any(a.get("kind", "human") == "human" for a in p["audio"])
    speech = any(a.get("kind") == "speech" for a in p["audio"])
    expected = (
        "HUMAN_AND_SPEECH"
        if human and speech
        else "GENERATED_SPEECH"
        if speech
        else "HUMAN_RECORDING"
        if human
        else "LICENSED_MUSIC"
        if p["music"]
        else "silent_sacred"
    )
    qa = result["qa"]
    judge = qa["judge"]
    required = {
        "dimensions",
        "codec",
        "duration",
        "size",
        "audio",
        "safe_window",
        "content",
        "handle",
        "follow_card",
        "source_images",
    }
    if (
        qa["audio"] != expected
        or judge["decision"] != "PASS"
        or judge.get("score") != 10
        or set(judge.get("criteria", {})) != required
        or not all(v is True for v in judge["criteria"].values())
        or len(judge.get("shots", [])) != 6
        or (qa.get("width"), qa.get("height"), qa.get("codec")) != (1080, 1920, "h264")
        or abs(qa.get("duration", 0) - p["duration"]) > 0.15
        or not 0 < qa.get("bytes", 0) <= 16_000_000
    ):
        raise ValueError("LEGACY_RESULT_AUDIO_OR_JUDGE_CHANGED")
    from studio_core.presentation.brand_frame import load_brand

    brand = load_brand()
    if (
        record["brand"]["clip_box"] != brand["clip_box"]
        or record["brand"]["overlay_sha256"] != brand["overlay"]["sha256"]
    ):
        raise ValueError("LEGACY_RESULT_FRAME_CHANGED")
    return float(p["duration"])
