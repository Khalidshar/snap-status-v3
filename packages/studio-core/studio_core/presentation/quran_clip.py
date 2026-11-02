"""Offline Quran/tafsir scenes in the pinned green frame. No delivery or HTTP."""

import json
import math
import re
import time
from pathlib import Path
from typing import Any

from studio_core.religious.quran_foundation import sha, verify_bundle
from studio_core.renderer import ensure_glyphs, proof, run, write_once

from . import brand_frame, quran_font, sacred_text


def pages(text: str, limit: int = 240) -> list[dict[str, Any]]:
    """Preserve source offsets, including paragraph separators, without rewriting."""
    result: list[dict[str, Any]] = []
    offset = 0
    for paragraph in re.split(r"(\n+)", text):
        if not paragraph or paragraph.startswith("\n"):
            offset += len(paragraph)
            continue
        parts = sacred_text.pages(paragraph, limit)
        for part in parts:
            if part:
                result.append({"text": part, "offset": offset, "length": len(part)})
            offset += len(part) + 1
        offset -= 1
    if not result or any(
        text[p["offset"] : p["offset"] + p["length"]] != p["text"] for p in result
    ):
        raise ValueError("QF_PAGINATION_CHANGED_TEXT")
    covered: set[int] = set()
    for page in result:
        covered.update(range(page["offset"], page["offset"] + page["length"]))
    if any(i not in covered and c not in {" ", "\n"} for i, c in enumerate(text)):
        raise ValueError("QF_PAGINATION_TRUNCATED_TEXT")
    return result


def _image(text: str, font: Path, target: Path, size: int, height: int) -> tuple[int, int]:
    ensure_glyphs(text, run(["/usr/bin/fc-query", "--format=%{charset}", str(font)], 5).stdout)
    return sacred_text.render_text_png(
        text,
        font,
        target,
        pango_view="/usr/bin/pango-view",
        width_px=820,
        font_size=size,
        max_height_px=height,
    )


def render(bundle: Path, app: str, *, output_name: str = "render") -> dict[str, Any]:
    isolation = proof("/run/madar-quran-no-egress.sock")
    value = verify_bundle(bundle, app, time.time())
    if value["expires_at"] - time.time() < 1200:
        raise ValueError("QF_INSUFFICIENT_RENDER_LIFETIME")
    if not re.fullmatch(r"render(?:-[1-9][0-9]*)?", output_name):
        raise ValueError("QF_RENDER_NAME_INVALID")
    root = bundle.parent / output_name
    if (root / "result.json").is_file():
        prior: dict[str, Any] = json.loads((root / "result.json").read_bytes())
        if prior.get("video_sha256") != sha((root / "video.mp4").read_bytes()):
            raise ValueError("QF_RENDER_CHANGED")
        return prior
    root.mkdir(mode=0o700, exist_ok=False)
    quran = quran_font.path()
    naskh = quran_font.font_for("hadith")
    verse_pages = pages(value["text"], 180)
    tafsir_pages = pages(value["tafsir"]["text"])
    # Every ayah file is complete. There is no seek, trim, shortest or TTS input.
    recitation_seconds = value["audio_duration"] or 0
    verse_seconds = max(recitation_seconds, sum(max(3, len(p["text"]) / 17) for p in verse_pages))
    total_chars = sum(len(p["text"]) for p in verse_pages)
    scenes = []
    for page in verse_pages:
        scenes.append(
            {**page, "kind": "ayah", "seconds": verse_seconds * len(page["text"]) / total_chars}
        )
    for page in tafsir_pages:
        scenes.append({**page, "kind": "tafsir", "seconds": max(4, len(page["text"]) / 17)})
    total = sum(s["seconds"] for s in scenes)
    if not math.isfinite(total) or total > brand_frame.MAX_SECONDS:
        raise ValueError("QF_WHOLE_AYAH_AND_TAFSIR_EXCEED_PLATFORM_LIMIT")
    sequence = []
    for i, scene in enumerate(scenes):
        folder = root / str(i)
        folder.mkdir(mode=0o700)
        ayah = scene["kind"] == "ayah"
        title = (
            f"سورة {value['surah']} — الآية {value['ayah']}" if ayah else value["tafsir"]["name"]
        )
        header = folder / "header.png"
        body = folder / "body.png"
        footer = folder / "footer.png"
        hsize = _image(title, naskh, header, 44, 120)
        bsize = _image(scene["text"], quran if ayah else naskh, body, 56 if ayah else 44, 1050)
        footer_text = (
            value["attribution"] if ayah else value["tafsir"]["name"] + " — المصدر: مؤسسة القرآن"
        )
        fsize = _image(footer_text, naskh, footer, 30, 180)
        graph = (
            f"[0:v][1:v]overlay=(W-w)/2:280[a];"
            f"[a][2:v]overlay=(W-w)/2:{470 + (1050 - bsize[1]) // 2}[b];"
            "[b][3:v]overlay=(W-w)/2:1620[v]"
        )
        # Explicit boxes inside the 1080x1920 source, then mapped by brand_frame.
        if hsize[0] > 820 or bsize[0] > 820 or fsize[0] > 820:
            raise ValueError("QF_TEXT_OUTSIDE_SAFE_AREA")
        image = folder / "scene.png"
        run(
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-n",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x0B3D2E:s=1080x1920",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(header),
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(body),
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(footer),
                "-filter_complex",
                graph,
                "-map",
                "[v]",
                "-frames:v",
                "1",
                "-threads",
                "1",
                str(image),
            ],
            60,
        )
        # Relative numeric-only paths; concat cannot load network or arbitrary input.
        sequence += [f"file '{i}/scene.png'", f"duration {scene['seconds']:.9f}"]
    sequence.append(f"file '{len(scenes) - 1}/scene.png'")
    timeline = root / "sequence.txt"
    write_once(timeline, ("\n".join(sequence) + "\n").encode())
    args = [
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
        "1",
        "-i",
        str(timeline),
    ]
    if value["audio_state"] == "HUMAN_RECITATION":
        args += [
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(bundle.parent / "recitation.mp3"),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-af",
            "apad",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
        ]
    else:
        args += ["-an"]
    clip = root / "clip.mp4"
    args += [
        "-vf",
        "fps=30,tpad=stop_mode=clone:stop_duration=180",
        "-t",
        f"{total:.9f}",
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-threads",
        "1",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        str(clip),
    ]
    run(args, 900)
    video = root / "video.mp4"
    composed = brand_frame.compose(clip, video, normalize_audio=False, timeout=900)
    if value["audio_state"] == "SILENT_AUDIO_FAILED":
        # The generic brand compositor adds an anullsrc stream. Remove it so the
        # religious fallback has no audio stream at all; video bytes are copied.
        silent = root / "silent.mp4"
        run(
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-n",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(video),
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-an",
                "-movflags",
                "+faststart",
                str(silent),
            ],
            30,
        )
        silent.replace(video)
    actual = brand_frame.probe("/usr/bin/ffprobe", video)
    if (
        abs(actual["duration"] - total) > 0.15
        or actual["duration"] + 0.05 < recitation_seconds
        or actual["audio"] != (value["audio_state"] == "HUMAN_RECITATION")
    ):
        raise ValueError("QF_FINAL_DURATION_OR_AUDIO_INVALID")
    if not 0 < video.stat().st_size <= 50_000_000:
        raise ValueError("QF_FINAL_SIZE_INVALID")
    for name, at in (("ayah", 0), ("tafsir", verse_seconds + 0.1)):
        run(
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-n",
                "-protocol_whitelist",
                "file,pipe",
                "-ss",
                str(at),
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-threads",
                "1",
                str(root / (name + ".png")),
            ],
            30,
        )
    # The full payload tree (including PNGs, video and text) shares the earliest expiry.
    verify_bundle(bundle, app, time.time())
    evidence = {
        **composed,
        "app": app,
        "job": value["job"],
        "expires_at": value["expires_at"],
        "video_sha256": sha(video.read_bytes()),
        "text_sha256": value["text_sha256"],
        "tafsir_sha256": value["tafsir"]["sha256"],
        "audio_policy": value["audio_state"],
        "recitation_seconds": recitation_seconds,
        "verse_seconds": verse_seconds,
        "duration": actual["duration"],
        "network_isolation": isolation,
        "safe_area": {"result": "PASS"},
        "scenes": scenes,
    }
    write_once(
        root / "result.json", json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()
    )
    return evidence
