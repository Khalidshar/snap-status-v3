"""Offline static-card primitive. Apps supply the layout; output is a silent review."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from studio_core.renderer.primitives import ensure_glyphs, proof, run, write_once


def render(
    labels: list[tuple[str, int, int]],
    root: Path,
    settings: dict[str, Any],
) -> dict[str, Any]:
    isolation = proof(settings["egress_socket"])
    # Filter expressions contain only validated paths and app-owned numeric positions.
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(root)):
        raise ValueError("Unsafe card workspace path")
    font, overlay = (Path(settings[k]).resolve(strict=True) for k in ("font", "overlay"))
    for path, key in ((font, "font_sha256"), (overlay, "overlay_sha256")):
        if (
            not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path))
            or hashlib.sha256(path.read_bytes()).hexdigest() != settings[key]
        ):
            raise ValueError("Card asset changed")
    version = run([settings["ffmpeg"], "-version"], 5).stdout.splitlines()[0]
    if version != settings["ffmpeg_version"]:
        raise ValueError("Card FFmpeg version changed")
    charset = run([settings["fc_query"], "--format=%{charset}", str(font)], 5).stdout
    filters = []
    for index, (text, size, y) in enumerate(labels):
        if (
            not text
            or len(text) > 90
            or any(ord(c) < 32 for c in text)
            or type(size) is not int
            or not 10 <= size <= 80
            or type(y) is not int
            or not 240 <= y <= 1640
        ):
            raise ValueError("Card label rejected")
        ensure_glyphs(text, charset)
        path = root / f"label-{index}.txt"
        write_once(path, text.encode())
        filters.append(
            f"drawtext=fontfile={font}:textfile={path}:expansion=none:text_shaping=1:"
            f"fontsize={size}:fontcolor=0xF6EEDE:x=(w-text_w)/2:y={y}"
        )
    graph = (
        "[0:v]" + ",".join(filters) + "[card];"
        "[card][1:v]overlay=0:0:format=auto,format=yuv420p[out]"
    )
    write_once(root / "filters.txt", graph.encode())
    output = root / "preview.mp4"
    run(
        [
            settings["ffmpeg"],
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-n",
            "-filter_complex_threads",
            "1",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x0B3D2E:s=1080x1920:r=24:d=8",
            "-i",
            str(overlay),
            "-filter_complex_script",
            str(root / "filters.txt"),
            "-map",
            "[out]",
            "-an",
            "-t",
            "8",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ],
        120,
    )
    metadata = json.loads(
        run(
            [
                settings["ffprobe"],
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(output),
            ],
            15,
        ).stdout
    )
    streams = metadata["streams"]
    duration = float(metadata["format"]["duration"])
    if (
        len(streams) != 1
        or streams[0]["codec_type"] != "video"
        or streams[0]["codec_name"] != "h264"
        or streams[0]["pix_fmt"] != "yuv420p"
        or streams[0]["width"] != 1080
        or streams[0]["height"] != 1920
        or not 7.9 <= duration <= 8.2
        or not 0 < output.stat().st_size <= 50_000_000
    ):
        raise ValueError("Card video QA failed")
    run(
        [
            settings["ffmpeg"],
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-ss",
            "1",
            "-i",
            str(output),
            "-frames:v",
            "1",
            "-threads",
            "1",
            str(root / "preview.png"),
        ],
        30,
    )
    return {
        "video_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "duration_seconds": duration,
        "width": 1080,
        "height": 1920,
        "network_isolation": isolation,
        "tool_version": version,
        "audio": "SILENT_REVIEW_ONLY",
        "eligible_for_delivery": False,
    }
