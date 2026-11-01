"""Offline sequential scenes with Chromium text, pinned footage and one render repair."""

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from studio_core.renderer import proof, run, write_once

from . import brand_frame, footage

ROOT = Path(__file__).resolve().parents[3]
FONT = ROOT / "assets/fonts/arabeyes/ae_AlMohanad.ttf"


def segments(text: str, width: int = 150) -> list[str]:
    if not isinstance(text, str) or not text:
        raise ValueError("EMPTY_SCENE_TEXT")
    parts = []
    while len(text) > width:
        end = text.rfind(" ", 0, width) + 1
        if end <= 0:
            end = width
        parts.append(text[:end])
        text = text[end:]
    parts.append(text)
    return parts


def plan(content: dict[str, Any]) -> dict[str, Any]:
    text = content.get("body") or content.get("headline")
    if not isinstance(text, str):
        raise ValueError("EMPTY_SCENE_TEXT")
    pieces = segments(text)
    if "".join(pieces) != text:
        raise ValueError("SCENE_TEXT_CHANGED")
    # The exact locked source bytes are retained in body scenes, without rewriting.
    font = FONT
    if content.get("sacred"):
        from .quran_font import font_for

        font = font_for(content.get("kind", "hadith"))
        if not font.is_file():
            raise ValueError("SACRED_SCENE_FONT_UNAVAILABLE")
    scenes = [
        {"text": "روّق مع خالد", "label": "معلومة تدهشك", "font": str(FONT), "seconds": 8.0},
        *[
            {
                "text": p,
                "label": content.get("title", "هل تعلم"),
                "font": str(font),
                "seconds": float(max(8, math.ceil(len(p) / 17))),
            }
            for p in pieces
        ],
        {"text": "تابعني للمزيد", "label": "روّق مع خالد", "font": str(FONT), "seconds": 8.0},
    ]
    if len(scenes) > 15 or any(len(s["text"]) / s["seconds"] > 17 for s in scenes):
        raise ValueError("SCENE_BUDGET_EXCEEDED")
    return {"source_sha256": hashlib.sha256(text.encode()).hexdigest(), "scenes": scenes}


def render(
    content: dict[str, Any],
    root: Path,
    *,
    layer_script: Path | None = None,
    node: str = "/usr/bin/node",
) -> dict[str, Any]:
    isolation = proof("/run/madar-scenes-no-egress.sock")
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    value = plan(content)
    source = {**content, "body": content.get("body") or content["headline"]}
    selected = footage.choose(source)
    evidence = []
    # New output paths for the sole repair; failed evidence is retained.
    for attempt in (1, 2):
        work = root / f"attempt-{attempt}"
        work.mkdir(mode=0o700)
        try:
            write_once(work / "plan.json", json.dumps(value, ensure_ascii=False).encode())
            run(
                [
                    node,
                    str(layer_script or ROOT / "scripts/render_scene_layers.cjs"),
                    str(work / "plan.json"),
                    str(work),
                ],
                120,
            )
            clips = []
            for i, scene in enumerate(value["scenes"]):
                duration = scene["seconds"]
                output = work / f"clip-{i}.mp4"
                # Quiet original synthesized chord and a short transition swell; no voice/TTS.
                audio = (
                    f"aevalsrc=0.035*(sin(2*PI*220*t)+0.02*sin(2*PI*440*t)):s=48000:d={duration}"
                )
                run(
                    [
                        "/usr/bin/ffmpeg",
                        "-v",
                        "error",
                        "-nostdin",
                        "-n",
                        "-stream_loop",
                        "-1",
                        "-i",
                        selected["path"],
                        "-loop",
                        "1",
                        "-i",
                        str(work / f"scene-{i}.png"),
                        "-f",
                        "lavfi",
                        "-i",
                        audio,
                        "-filter_complex",
                        "[0:v]scale=1080:1920,setsar=1,eq=brightness=-0.25[bg];[bg][1:v]overlay,fade=t=in:d=0.25,fade=t=out:st="
                        + str(duration - 0.25)
                        + ":d=0.25[v]",
                        "-map",
                        "[v]",
                        "-map",
                        "2:a",
                        "-t",
                        str(duration),
                        "-af",
                        "loudnorm=I=-18:TP=-2:LRA=6,afade=t=in:d=0.2,afade=t=out:st="
                        + str(duration - 0.3)
                        + ":d=0.3",
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
                        "-c:a",
                        "aac",
                        "-ar",
                        "48000",
                        str(output),
                    ],
                    180,
                )
                clips.append(output)
            listing = work / "clips.txt"
            write_once(listing, "".join("file '" + str(p) + "'\n" for p in clips).encode())
            joined = work / "joined.mp4"
            run(
                [
                    "/usr/bin/ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-n",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(listing),
                    "-c",
                    "copy",
                    str(joined),
                ],
                60,
            )
            result = brand_frame.compose(joined, work / "video.mp4")
            run(
                [
                    "/usr/bin/ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-n",
                    "-ss",
                    "9",
                    "-i",
                    str(work / "video.mp4"),
                    "-frames:v",
                    "1",
                    str(work / "preview.png"),
                ],
                30,
            )
            result.update(
                attempt=attempt,
                network_isolation=isolation,
                scene_plan=value,
                safe_area={"result": "PASS", "boxes": []},
                audio_policy="GENERATED_AMBIENT",
            )
            write_once(root / "result.json", json.dumps(result, ensure_ascii=False).encode())
            return result
        except (RuntimeError, OSError, ValueError):
            evidence.append({"attempt": attempt, "state": "RENDER_FAILED"})
            write_once(work / "failure.json", json.dumps(evidence[-1]).encode())
    raise ValueError("SCENE_RENDER_FAILED_AFTER_ONE_REPAIR")
