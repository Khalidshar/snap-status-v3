"""Opt-in adapter for existing owned content pipelines, not a delivery bypass."""

import json
import math
import os
import re
import stat
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from studio_core.config import Config
from studio_core.network import NetworkClient
from studio_core.presentation import brand_frame, faith, quran_font
from studio_core.presentation.did_you_know import load_kind, make_content, validate_content
from studio_core.religious.quran_commands import QuranSocketPolicy
from studio_core.religious.quran_foundation import (
    CATALOG,
    Source,
    Store,
    download_ayah,
    exact_ayah,
    safe_root,
    sha,
)
from studio_core.renderer import proof, run, write_once

BASE = Path("/var/lib/madar")
KINDS = {"verse", "friday", "kahf"}


def short(app: str) -> str:
    if app not in {"snap-bot-v2", "status-bot-v2"}:
        raise ValueError("QF_APP_INVALID")
    return "snap" if app == "snap-bot-v2" else "status"


def store_for(app: str) -> Store:
    return Store(BASE / ("quran-" + short(app)), app)


def references(content: dict[str, Any]) -> list[tuple[int, int]]:
    validate_content(content)
    if content.get("kind") not in KINDS:
        raise ValueError("QF_NOT_QURAN_COMMAND")
    item = next(i for i in faith.load(content["kind"]) if i["id"] == content["fact_id"])
    refs = [(item["surah"], a) for a in item["ayahs"]]
    if " ".join(exact_ayah(s, a) for s, a in refs) != content["body"]:
        raise ValueError("QF_COMMAND_TEXT_CHANGED")
    return refs


def prepare_selection(
    store: Store, source: Source, job: str, content: dict[str, Any], catalog: Path = CATALOG
) -> Path:
    refs = references(content)
    store.sweep()
    reciter, expires = store.reserve(job, source.catalog(json.loads(catalog.read_text())))
    directory = store.directory(job)
    try:
        bundles = []
        for index, (surah, ayah) in enumerate(refs):
            # One reservation/reader for the whole clip; every ayah recording stays whole.
            path = download_ayah(
                store, source, job, surah, ayah, reciter, expires, directory / str(index)
            )
            bundles.append({"file": f"{index}/bundle.json", "sha256": sha(path.read_bytes())})
        selection = directory / "selection.json"
        value = {
            "schema": 1,
            "app": store.app,
            "job": job,
            "content_hash": content["content_hash"],
            "text_sha256": sha(content["body"].encode()),
            "reciter": reciter,
            "expires_at": expires,
            "bundles": bundles,
        }
        write_once(selection, json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
        return selection
    except Exception:
        store.failed(job)
        raise


def prepare_command(app: str, job: str) -> Path:
    store = store_for(app)
    if not re.fullmatch(r"[0-9a-f]{32}", job):
        raise ValueError("QF_JOB_INVALID")
    request = json.loads((store.root / "requests" / (job + ".json")).read_bytes())
    if request.get("app") != app or request.get("job") != job:
        raise ValueError("QF_COMMAND_SCOPE_CHANGED")
    socket = Path("/run/madar/" + short(app) + "-quran.sock")
    client = NetworkClient(cast(Config, QuranSocketPolicy(socket)))
    ready = json.loads(client.get("quran://ready").body)
    return prepare_selection(store, Source(client, ready["environment"]), job, request["content"])


def stage(content: dict[str, Any], app: str, job: str, input_path: Path) -> bool:
    if content.get("legacy_production"):
        from studio_core.production.pipeline import stage as stage_production

        stage_production(content, app, job, input_path)
        return True
    # Default-off means no probing, filesystem changes, downloads or altered content.
    if os.environ.get("MADAR_QURAN_AUDIO") != "1" or content.get("kind") not in KINDS:
        return False
    try:
        socket = Path("/run/madar/" + short(app) + "-quran.sock")
        client = NetworkClient(cast(Config, QuranSocketPolicy(socket)))
        if json.loads(client.get("quran://ready").body).get("configured") is not True:
            return False
    except (OSError, ValueError, RuntimeError):
        # Missing keys/service has exactly the legacy behavior, even with the flag on.
        return False
    references(content)
    store = store_for(app)
    requests = safe_root(store.root / "requests")
    write_once(
        requests / (job + ".json"),
        json.dumps(
            {"app": app, "job": job, "content": content},
            ensure_ascii=False,
            sort_keys=True,
        ).encode(),
    )
    unit = "madar-v3-" + short(app) + "-quran-prepare@" + job + ".service"
    argv = ["/usr/bin/systemctl", "start", unit]
    if os.geteuid() != 0:
        # Reuse the existing render coordinator's systemd-run permission.
        argv = [
            "/usr/bin/sudo",
            "-n",
            "/usr/bin/systemd-run",
            "--quiet",
            "--wait",
            "--collect",
            *argv,
        ]
    try:
        subprocess.run(argv, check=True, capture_output=True, timeout=900)  # noqa: S603
    except Exception:
        store.failed(job)
        raise
    selected = store.directory(job) / "selection.json"
    value = json.loads(selected.read_bytes())
    if value["content_hash"] != content["content_hash"] or value["app"] != app:
        raise ValueError("QF_SELECTION_CHANGED")
    write_once(
        input_path.with_name("quran.json"),
        json.dumps(
            {"selection_sha256": sha(selected.read_bytes()), "app": app, "job": job},
            sort_keys=True,
        ).encode(),
    )
    return True


def track(result: dict[str, Any], path: Path, digest: str) -> None:
    if result.get("legacy_production"):
        from studio_core.production.pipeline import track_qf

        track_qf(result["legacy_production"]["content"]["legacy_production"], path, digest=digest)
        return
    record = result.get("quran_foundation")
    if record:
        store_for(record["app"]).track(path, result["job_id"], record["expires_at"], digest)


def track_bundle(result: dict[str, Any], directory: Path, preview_sha: str) -> None:
    if not result.get("quran_foundation") and not result.get("legacy_production"):
        return
    track(result, directory / "did-you-know.mp4", result["video_sha256"])
    track(result, directory / "preview.png", preview_sha)
    track(
        result,
        directory / "state/renders" / result["job_id"] / "output/did-you-know.mp4",
        result["video_sha256"],
    )


def check_result(result: dict[str, Any]) -> float:
    record = result["quran_foundation"]
    fact = next((f for f in load_kind(record["kind"]) if f["id"] == record["fact_id"]), None)
    if record["kind"] not in KINDS or fact is None:
        raise ValueError("QF_RESULT_KIND_CHANGED")
    content = make_content(fact, time.time(), record["kind"])
    expected = sha(content["body"].encode())
    duration, recitation = record["duration"], record["recitation_seconds"]
    if (
        record["text_sha256"] != expected
        or content["content_hash"] != result["content_hash"]
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or not 0 < duration <= 180
        or not isinstance(recitation, (int, float))
        or not math.isfinite(recitation)
        or not 0 <= recitation <= duration
        or record["quran_font_sha256"] != quran_font.FONT_SHA256
        or record["tafsir_font_sha256"] != quran_font.NASKH_SHA256
        or record["expires_at"] <= time.time()
        or record["audio_policy"] not in {"HUMAN_RECITATION", "SILENT_AUDIO_FAILED"}
        or result["qa"]["audio"] != record["audio_policy"]
        or (record["audio_policy"] == "SILENT_AUDIO_FAILED" and recitation != 0)
    ):
        raise ValueError("QF_RESULT_INVALID_OR_EXPIRED")
    return float(duration)


def _read(path: Path, limit: int, uid: int) -> bytes:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("QF_UNSAFE_RENDER_INPUT")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        meta = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != uid
            or meta.st_mode & 0o037
            or not 0 < meta.st_size <= limit
        ):
            raise ValueError("QF_RENDER_INPUT_INVALID")
        return stream.read(limit + 1)


def _render(
    config: Config, settings: dict[str, Any], job_id: str, content_hash: str, attempt: int
) -> dict[str, Any]:
    from studio_core.presentation.render import render as legacy

    if not re.fullmatch(r"[0-9a-f]{32}", job_id) or type(attempt) is not int or attempt < 1:
        raise ValueError("QF_RENDER_ID_INVALID")
    source = (
        Path(settings["input_root"]) / job_id / "attempts" / str(attempt) / "input/content.json"
    )
    marker = source.with_name("quran.json")
    if source.with_name("legacy.json").is_file():
        from studio_core.production.pipeline import render as render_production

        return render_production(config, settings, job_id, content_hash, attempt)
    if not marker.exists():
        return legacy(config, settings, job_id, content_hash, attempt)
    isolation = proof(settings["egress_socket"])
    content = json.loads(_read(source, 16384, settings["worker_uid"]))
    references(content)
    if content["content_hash"] != content_hash or content["expires_at"] <= time.time():
        raise ValueError("QF_RENDER_CONTENT_CHANGED")
    mark = json.loads(_read(marker, 4096, settings["worker_uid"]))
    if mark.get("app") != config.app or mark.get("job") != job_id:
        raise ValueError("QF_RENDER_SCOPE_CHANGED")
    store = store_for(config.app)
    selected = store.directory(job_id) / "selection.json"
    if sha(selected.read_bytes()) != mark["selection_sha256"]:
        raise ValueError("QF_SELECTION_CHANGED")
    value = json.loads(selected.read_bytes())
    if (
        value["app"] != config.app
        or value["job"] != job_id
        or value["content_hash"] != content_hash
    ):
        raise ValueError("QF_SELECTION_SCOPE_CHANGED")
    from studio_core.presentation.quran_clip import render as render_ayah

    parts = []
    texts = []
    for index, entry in enumerate(value["bundles"]):
        if entry["file"] != f"{index}/bundle.json":
            raise ValueError("QF_BUNDLE_PATH_CHANGED")
        path = selected.parent / entry["file"]
        if sha(path.read_bytes()) != entry["sha256"]:
            raise ValueError("QF_BUNDLE_CHANGED")
        bundle = json.loads(path.read_bytes())
        if bundle["reciter"] != value["reciter"]:
            raise ValueError("QF_READER_CHANGED_INSIDE_CLIP")
        texts.append(bundle["text"])
        parts.append(render_ayah(path, config.app, output_name=f"render-{attempt}"))
    if " ".join(texts) != content["body"] or not parts:
        raise ValueError("QF_RENDER_TEXT_TRUNCATED")
    duration = sum(p["duration"] for p in parts)
    if duration > 180:
        raise ValueError("QF_WHOLE_AYAH_AND_TAFSIR_EXCEED_PLATFORM_LIMIT")
    human = all(p["audio_policy"] == "HUMAN_RECITATION" for p in parts)
    work = safe_root(selected.parent / f"combined-{attempt}")
    video = work / "video.mp4"
    if not video.exists():
        listing = work / "clips.txt"
        write_once(
            listing,
            (
                "\n".join(f"file '../{i}/render-{attempt}/video.mp4'" for i in range(len(parts)))
                + "\n"
            ).encode(),
        )
        run(
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-n",
                "-protocol_whitelist",
                "file,pipe",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                *(["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"] if human else ["-an"]),
                "-movflags",
                "+faststart",
                str(video),
            ],
            180,
        )
    measured = brand_frame.probe("/usr/bin/ffprobe", video)
    if (
        abs(measured["duration"] - duration) > 0.15
        or measured["audio"] != human
        or (measured["width"], measured["height"], measured["codec"]) != (1080, 1920, "h264")
        or not 0 < video.stat().st_size <= settings["profile"]["max_bytes"]
    ):
        raise ValueError("QF_COMBINED_MEDIA_INVALID")
    spool = Path(settings["spool_root"])
    if not spool.is_absolute() or spool.name != "renders":
        raise ValueError("QF_SPOOL_INVALID")
    output = safe_root(spool / job_id / "attempts" / str(attempt) / "output")
    profile = settings["profile"]
    version = run([profile["ffmpeg"], "-version"], 5).stdout.splitlines()[0]
    if version != profile["ffmpeg_version"]:
        raise ValueError("QF_TOOL_VERSION_CHANGED")
    record = {
        "app": config.app,
        "kind": content["kind"],
        "fact_id": content["fact_id"],
        "duration": measured["duration"],
        "text_sha256": sha(content["body"].encode()),
        "expires_at": value["expires_at"],
        "reciter": value["reciter"],
        "quran_font_sha256": quran_font.FONT_SHA256,
        "tafsir_font_sha256": quran_font.NASKH_SHA256,
        "recitation_seconds": sum(p["recitation_seconds"] for p in parts) if human else 0,
        "audio_policy": "HUMAN_RECITATION" if human else "SILENT_AUDIO_FAILED",
    }
    result = {
        "job_id": job_id,
        "attempt": attempt,
        "content_hash": content_hash,
        "content_type": "did_you_know",
        "artifact": f"renders/{job_id}/attempts/{attempt}/output/did-you-know.mp4",
        "video_sha256": sha(video.read_bytes()),
        "font_sha256": profile["font_sha256"],
        "latin_font_sha256": profile["latin_font_sha256"],
        "tool_version": version,
        "glyph_coverage": "PASS",
        "network_isolation": isolation,
        "safe_area": {"result": "PASS", "boxes": []},
        "quran_foundation": record,
        "qa": {
            "result": "PASS",
            "bytes": video.stat().st_size,
            "width": measured["width"],
            "height": measured["height"],
            "codec": measured["codec"],
            "duration": measured["duration"],
            "audio": record["audio_policy"],
        },
    }
    check_result(result)
    for original, target in (
        (video, output / "did-you-know.mp4"),
        (Path(parts[0]["output"]).parent / "ayah.png", output / "preview.png"),
    ):
        track(result, target, sha(original.read_bytes()))
        if target.exists():
            if sha(target.read_bytes()) != sha(original.read_bytes()):
                raise ValueError("QF_OUTPUT_CHANGED")
        else:
            os.link(original, target)
    write_once(
        output / "render-result.json",
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2).encode(),
    )
    store.rendered(job_id)
    return result


def render(
    config: Config, settings: dict[str, Any], job_id: str, content_hash: str, attempt: int
) -> dict[str, Any]:
    try:
        return _render(config, settings, job_id, content_hash, attempt)
    except Exception:
        marker = (
            Path(settings["input_root"]) / job_id / "attempts" / str(attempt) / "input/quran.json"
        )
        if marker.is_file():
            with suppress(OSError, ValueError):
                store_for(config.app).failed(job_id)
        raise
