"""Local FFmpeg renderer. Run only in the no-network systemd unit."""

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from studio_core.config import Config
from studio_core.jobs import Repository, canonical
from studio_core.jobs.component_lock import ComponentLocks

# Shared renderer primitives (core 0.10.0); re-exported so callers and patches keep working.
from studio_core.renderer import ensure_glyphs, proof, run
from studio_core.storage import Storage

from .weather import content_hash as calculate_content_hash


def render(
    config: Config, settings: dict[str, Any], job_id: str, content_hash: str
) -> dict[str, Any]:
    if config.app != "status-bot-v2":
        raise ValueError("STATUS_SCOPE_INVALID")
    repo = Repository(config)
    job = repo.get(job_id)
    if not job or job["app"] != config.app or job["state"] != "RENDERING":
        raise ValueError("STATUS_RENDER_OWNER_INVALID")
    owned = repo.assert_owner(job_id, job["lease_token"])
    locks = ComponentLocks(repo)
    with locks.hold(
        "weather-render",
        "USE",
        min(1200, settings.get("profile", {}).get("timeout_seconds", 240) + 90),
    ):
        return _render(
            config, settings, job_id, content_hash, repo, job["lease_token"], owned["attempt"]
        )


def _render(
    config: Config,
    settings: dict[str, Any],
    job_id: str,
    content_hash: str,
    repo: Repository,
    token: str,
    attempt: int,
) -> dict[str, Any]:
    isolation = proof(settings["egress_socket"])
    root = Storage(config).workspace(job_id)
    content_path = root / "input/content.json"
    if content_path.is_symlink():
        raise ValueError("Unsafe content path")
    content = json.loads(content_path.read_text())
    digest = calculate_content_hash(content)
    if (
        digest != content_hash
        or content.get("content_hash") != digest
        or content.get("type") != "weather"
        or content.get("sacred") is not False
    ):
        raise ValueError("Unverified render input")
    if content["expires_at"] <= time.time():
        raise ValueError("Expired content")
    profile = settings["profile"]
    font = Path(profile["font"])
    latin_font = Path(profile["latin_font"])
    if hashlib.sha256(font.read_bytes()).hexdigest() != profile["font_sha256"]:
        raise ValueError("Font version mismatch")
    if hashlib.sha256(latin_font.read_bytes()).hexdigest() != profile["latin_font_sha256"]:
        raise ValueError("Latin font version mismatch")
    charsets = {
        font: run([profile["fc_query"], "--format=%{charset}", str(font)], 5).stdout,
        latin_font: run([profile["fc_query"], "--format=%{charset}", str(latin_font)], 5).stdout,
    }
    version = run([profile["ffmpeg"], "-version"], 5).stdout.splitlines()[0]
    if version != profile["ffmpeg_version"]:
        raise ValueError("FFmpeg version mismatch")
    labels = [
        ("STATUS STUDIO / V2", 52, 210, "0x76E4C2"),
        (content["title"], 108, 440, "white"),
        (f"{content['temperature_c']:.1f}°", 166, 720, "0x76E4C2"),
        ("درجة مئوية", 62, 940, "white"),
        (f"الرياح {content['wind_kmh']:.1f} كم في الساعة", 60, 1080, "white"),
        ("تقدير نموذجي للطقس", 44, 1300, "0xCBD5E1"),
        (content["model_time"][:16].replace("T", " "), 36, 1380, "0xCBD5E1"),
        (content["attribution"], 30, 1490, "0xCBD5E1"),
    ]
    filters = [
        "drawbox=x=80:y=340:w=920:h=1240:color=0x142D41:t=fill",
        "drawbox=x=80:y=340:w=8:h=1240:color=0x76E4C2:t=fill",
    ]
    for index, (text, size, y, color) in enumerate(labels):
        if len(text) > 110 or any(ord(c) < 32 for c in text):
            raise ValueError("Unsafe label")
        label_font = latin_font if index in (0, 2, 6, 7) else font
        ensure_glyphs(text, charsets[label_font])
        path = root / "design" / f"text{index}.txt"
        path.write_text(text, encoding="utf-8")
        filters.append(
            f"drawtext=fontfile={label_font}:textfile={path}:expansion=none:text_shaping=1:fontsize={size}:fontcolor={color}:x=(w-text_w)/2:y={y}"
        )
    filter_path = root / "design/filters.txt"
    filter_path.write_text(",".join(filters))
    output = root / "output/weather.mp4"
    partial = root / "temp/weather.partial.mp4"
    if any(
        path.exists() or path.is_symlink()
        for path in (
            output,
            partial,
            root / "output/preview.png",
            root / "output/render-result.json",
        )
    ):
        raise ValueError("STATUS_RENDER_REPLAY_REQUIRES_RECONCILIATION")
    args = [
        profile["ffmpeg"],
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-threads",
        "1",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x081724:s={profile['width']}x{profile['height']}:r={profile['fps']}:d={profile['duration_seconds']}",
        "-filter_script:v",
        str(filter_path),
        "-an",
        "-c:v",
        "libx264",
        "-threads",
        "1",
        "-preset",
        profile["preset"],
        "-crf",
        str(profile["crf"]),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(partial),
    ]
    run(args, profile["timeout_seconds"])
    qa = validate(partial, profile)
    preview = root / "temp/preview.partial.png"
    run(
        [
            profile["ffmpeg"],
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-ss",
            "1",
            "-i",
            str(partial),
            "-frames:v",
            "1",
            str(preview),
        ],
        30,
    )
    result = {
        "job_id": job_id,
        "content_hash": content_hash,
        "video_sha256": hashlib.sha256(partial.read_bytes()).hexdigest(),
        "qa": qa,
        "network_isolation": isolation,
        "tool_version": version,
        "font_sha256": profile["font_sha256"],
        "latin_font_sha256": profile["latin_font_sha256"],
        "glyph_coverage": "PASS",
    }
    encoded_result = json.dumps(result, ensure_ascii=False, indent=2)
    with repo.owned_transaction(job_id, token, attempt=attempt):
        partial.replace(output)
        preview.replace(root / "output/preview.png")
        (root / "output/render-result.json").write_text(encoded_result)
    # Partial artifacts are retained on a failed/stale commit for reconciliation.

    return result


def validate(path: Path, profile: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(
        run(
            [
                profile["ffprobe"],
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            30,
        ).stdout
    )
    videos = [s for s in value["streams"] if s["codec_type"] == "video"]
    if len(videos) != 1:
        raise ValueError("Video stream missing")
    stream = videos[0]
    duration = float(value["format"]["duration"])
    if (
        stream["width"] != profile["width"]
        or stream["height"] != profile["height"]
        or stream["codec_name"] != "h264"
        or stream["pix_fmt"] != "yuv420p"
    ):
        raise ValueError("Invalid video profile")
    if (
        abs(duration - profile["duration_seconds"]) > 0.25
        or not 0 < path.stat().st_size <= profile["max_bytes"]
    ):
        raise ValueError("Invalid duration/size")
    run(
        [
            profile["ffmpeg"],
            "-v",
            "error",
            "-xerror",
            "-nostdin",
            "-i",
            str(path),
            "-f",
            "null",
            "-",
        ],
        profile["timeout_seconds"],
    )
    return {
        "result": "PASS",
        "width": stream["width"],
        "height": stream["height"],
        "duration": duration,
        "bytes": path.stat().st_size,
        "codec": "h264",
        "audio": "not_required_for_weather_card",
        "visual_review": "PENDING",
    }
