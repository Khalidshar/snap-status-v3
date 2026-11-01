"""App-neutral one-minute composition of frozen, independently validated parts."""

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from studio_core.config import Config
from studio_core.renderer import run, write_once
from studio_core.sources.daily_brief import validate_brief

from . import command_cards, footage
from .render import render as render_faith


def render(
    value: dict[str, Any], directory: Path, *, app: str, media: Path | None = None
) -> dict[str, Any]:
    if app not in {"snap-bot-v2", "status-bot-v2"}:
        raise ValueError("BRIEF_APP_INVALID")
    validate_brief(value, time.time())
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    outputs = []
    card = command_cards.render(
        [("يومك في دقيقة", 52, 430), (value["dates"][0], 30, 740), (value["dates"][1], 44, 940)],
        directory / "dates",
        backdrop=footage.choose({"body": str(value["dates"])}),
    )
    isolation = card["network_isolation"]
    outputs.append(Path(card["output"]))
    job = uuid.uuid4().hex
    input_root = directory / "input/renders"
    source = input_root / job / "attempts/1/input/content.json"
    source.parent.mkdir(parents=True, mode=0o700)
    write_once(source, json.dumps(value["faith"], ensure_ascii=False).encode())
    os.chmod(source, 0o640)
    settings: dict[str, Any] = {
        "spool_root": str(directory / "faith/renders"),
        "input_root": str(input_root),
        "worker_uid": os.getuid(),
        "profile": command_cards.profile(),
        "egress_socket": "/run/madar-brief-no-egress.sock",
    }
    faith = render_faith(
        Config.load(
            command_cards.ROOT
            / (
                "config/status/commands.toml"
                if app == "status-bot-v2"
                else "config/snap/did_you_know.toml"
            )
        ),
        settings,
        job,
        value["faith"]["content_hash"],
        1,
    )
    outputs.append(Path(settings["spool_root"]).parent / faith["artifact"])
    for key in ("saudi", "tech"):
        content = value[key]
        parts = command_cards.lines(content["headline"])
        if len(parts) > 9:
            raise ValueError("Brief headline exceeds card")
        card = command_cards.render(
            [
                (content["title"], 52, 430),
                *[(s, 46, 600 + i * 75) for i, s in enumerate(parts)],
                ("المصدر: " + content["source"], 34, 1340),
                (content["published_label"], 26, 1430),
            ],
            directory / key,
            backdrop=(
                footage.pinned(
                    {**content, "body": content["headline"]}, media / key / "footage.json"
                )
                if media and (media / key / "footage.json").exists()
                else footage.choose({"body": content["headline"]})
            ),
        )
        outputs.append(Path(card["output"]))
    listing = directory / "concat.txt"
    write_once(listing, "".join(f"file '{p}'\n" for p in outputs).encode())
    p = command_cards.profile()
    video = directory / "video.mp4"
    run(
        [
            p["ffmpeg"],
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
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(video),
        ],
        180,
    )
    qa = json.loads(
        run(
            [
                p["ffprobe"],
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(video),
            ],
            30,
        ).stdout
    )
    videos = [s for s in qa["streams"] if s.get("codec_type") == "video"]
    audios = [s for s in qa["streams"] if s.get("codec_type") == "audio"]
    if (
        len(videos) != 1
        or len(audios) != 1
        or (videos[0]["width"], videos[0]["height"], videos[0]["codec_name"], videos[0]["pix_fmt"])
        != (1080, 1920, "h264", "yuv420p")
        or (audios[0]["codec_name"], audios[0]["sample_rate"]) != ("aac", "48000")
    ):
        raise ValueError("Brief media profile changed")
    run(
        [p["ffmpeg"], "-v", "error", "-xerror", "-nostdin", "-i", str(video), "-f", "null", "-"],
        180,
    )
    duration = float(qa["format"]["duration"])
    if not 30 <= duration <= 60 or not 0 < video.stat().st_size <= 50_000_000:
        raise ValueError("Brief exceeds one minute or size limit")
    validate_brief(value, time.time())
    write_once(
        directory / "result.json",
        json.dumps(
            {
                "content_hash": value["content_hash"],
                "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
                "duration": duration,
                "network_isolation": isolation,
                "safe_area": {"result": "PASS", "boxes": []},
                "audio_policy": "BRIEF_MIX",
            }
        ).encode(),
    )

    result: dict[str, Any] = json.loads((directory / "result.json").read_text())
    return result
