"""Pinned offline renderer for owner-requested text cards, with pixel admission."""

import hashlib
import re
import textwrap
import tomllib
from pathlib import Path
from typing import Any

from studio_core.renderer import ensure_glyphs, proof, run, write_once

from . import brand_frame
from .fact_layout import display_text, measure_label
from .font_admission import font_paths

ROOT = Path(__file__).resolve().parents[3]


def profile() -> dict[str, Any]:
    return dict(
        tomllib.loads((ROOT / "config/snap/did_you_know.toml").read_text())["slice"]["profile"]
    )


def lines(text: str, width: int = 26) -> list[str]:
    parts = textwrap.wrap(
        text,
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
        replace_whitespace=False,
        drop_whitespace=False,
    )
    if not parts or "".join(parts) != text:
        raise ValueError("Card text changed")
    return parts


def render(
    labels: list[tuple[str, int, int]],
    root: Path,
    *,
    settings: dict[str, Any] | None = None,
    backdrop: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = settings or profile()
    isolation = proof("/run/madar-command-card-no-egress.sock")
    if root.is_symlink() or any(p.is_symlink() for p in root.absolute().parents):
        raise ValueError("Unsafe card workspace")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(root.absolute())):
        raise ValueError("Unsafe card path")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    font, _ = font_paths(settings)
    version = run([settings["ffmpeg"], "-version"], 5).stdout.splitlines()[0]
    if version != settings["ffmpeg_version"]:
        raise ValueError("Card tool version changed")
    charset = run([settings["fc_query"], "--format=%{charset}", str(font)], 5).stdout
    filters = []
    if backdrop:
        from .footage import filters as backdrop_filters

        if hashlib.sha256(Path(backdrop["path"]).read_bytes()).hexdigest() != backdrop["sha256"]:
            raise ValueError("Background changed")
        filters.extend(backdrop_filters(24))
    boxes = []
    for i, (text, requested, y) in enumerate(labels):
        if not text or len(text) > 100 or any(ord(c) < 32 for c in text):
            raise ValueError("Unsafe card label")
        ensure_glyphs(text, charset)
        path = root / f"label-{i}.txt"
        write_once(path, display_text(text, "arabic").encode())
        for size in range(min(requested, 60), 17, -2):
            value = (
                f"drawtext=fontfile={font}:textfile={path}:expansion=none:text_shaping=1:"
                f"fontsize={size}:fontcolor=0xF6EEDE:x=(w-text_w)/2:y={y}"
            )
            try:
                box = measure_label(settings, value)
                break
            except ValueError:
                continue
        else:
            raise ValueError("Card label cannot fit SAFE")
        boxes.append(box)
        filters.append(value)
    graph = root / "filters.txt"
    write_once(graph, ",".join(filters).encode())
    clip = root / "card.mp4"
    run(
        [
            settings["ffmpeg"],
            "-v",
            "error",
            "-nostdin",
            "-n",
            *(
                ["-stream_loop", "-1", "-i", backdrop["path"]]
                if backdrop
                else ["-f", "lavfi", "-i", "color=c=0x0B3D2E:s=1080x1920:r=24:d=8"]
            ),
            "-filter_script:v",
            str(graph),
            "-t",
            "8",
            "-an",
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
            str(clip),
        ],
        180,
    )
    video = root / "video.mp4"
    result = brand_frame.compose(
        clip, video, ffmpeg=settings["ffmpeg"], ffprobe=settings["ffprobe"], normalize_audio=False
    )
    if not 0 < video.stat().st_size <= 50_000_000:
        raise ValueError("Card video exceeds size limit")
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
            str(video),
            "-frames:v",
            "1",
            "-threads",
            "1",
            str(root / "preview.png"),
        ],
        30,
    )
    return {
        **result,
        "font_sha256": hashlib.sha256(font.read_bytes()).hexdigest(),
        "network_isolation": isolation,
        "safe_area": {"result": "PASS", "boxes": boxes},
        "audio_policy": "SILENT",
        "tool_version": version,
    }


def original(content: dict[str, Any], manifest: Path, root: Path) -> dict[str, Any]:
    """Preserve the whole event video and its audio inside the approved window."""
    from studio_core.sources.event_video import verify

    source = verify(content, manifest)
    root.mkdir(mode=0o700)
    isolation = proof("/run/denied-source-video.sock")
    result = brand_frame.compose(source, root / "video.mp4", normalize_audio=False)
    run(
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-i",
            str(root / "video.mp4"),
            "-frames:v",
            "1",
            str(root / "preview.png"),
        ],
        30,
    )
    result.update(
        network_isolation=isolation,
        safe_area={"result": "PASS", "boxes": []},
        audio_policy="ORIGINAL_SOURCE",
    )
    return result
