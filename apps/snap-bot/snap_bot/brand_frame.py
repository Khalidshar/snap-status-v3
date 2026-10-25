"""Compose a clip inside the approved "روّق مع خالد" personal frame (owner decision 2026-10-24).

The brand text is never drawn here. Chromium rendered it once into a pinned transparent
overlay (scripts/render_brand_frame.js), because the pinned FFmpeg mis-shapes Aref Ruqaa.
FFmpeg only scales the clip into the frame's box, lays the overlay on top and encodes for
Snapchat and WhatsApp HD. Local files only: no network and no delivery.
Usage: python -m snap_bot.brand_frame CLIP.mp4 OUT.mp4
"""

import argparse
import hashlib
import json
import os
import struct
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from studio_core.renderer import run

ROOT = Path(__file__).resolve().parents[3]
BRAND = ROOT / "assets/brand/rawwag"
WIDTH, HEIGHT = 1080, 1920
MAX_SECONDS = 180.0
# Highest quality Snapchat and WhatsApp (HD) both accept.
ENCODE = [
    "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-profile:v", "high",
    "-level:v", "4.1", "-maxrate", "8M", "-bufsize", "16M", "-r", "30", "-g", "60",
    "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
    "-color_trc", "bt709", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
    "-movflags", "+faststart",
]  # fmt: skip


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError("Brand overlay is not a PNG")
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def pinned(root: Path, name: str, expected: str) -> Path:
    path = root / name
    if path.is_symlink() or not path.is_file() or sha256(path) != expected:
        raise ValueError("Brand file changed: " + Path(name).name)
    return path


def load_brand(root: Path = BRAND) -> dict[str, Any]:
    """The approved frame, refused unless every pinned file still matches."""
    brand: dict[str, Any] = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    overlay = brand["overlay"]
    pinned(root, brand["template"]["file"], brand["template"]["sha256"])
    for font in brand["fonts"]:
        pinned(root, font["file"], font["sha256"])
        pinned(root, font["license_file"], font["license_sha256"])
    path = pinned(root, overlay["file"], overlay["sha256"])
    if png_size(path) != (WIDTH, HEIGHT) or (overlay["width"], overlay["height"]) != (
        WIDTH,
        HEIGHT,
    ):
        raise ValueError("Brand overlay must be 1080x1920")
    box = brand["clip_box"]
    if not (
        box["x"] > 0
        and box["y"] > 0
        and box["x"] + box["width"] < WIDTH
        and box["y"] + box["height"] < HEIGHT
    ):
        raise ValueError("Clip box outside the frame")
    return {**brand, "overlay_path": path}


def probe(ffprobe: str, path: Path) -> dict[str, Any]:
    value = json.loads(
        run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            30,
        ).stdout
    )
    streams = value.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ValueError("Clip has no video stream")
    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "codec": video.get("codec_name"),
        "duration": float(value["format"]["duration"]),
        "audio": audio is not None,
        "audio_codec": audio and audio.get("codec_name"),
        "sample_rate": audio and int(audio.get("sample_rate", 0)),
    }


def command(
    ffmpeg: str, clip: Path, out: Path, brand: dict[str, Any], duration: float, audio: bool
) -> list[str]:
    box, bg = brand["clip_box"], brand["background"]
    gradient = (
        f"gradients=s={WIDTH}x{HEIGHT}:c0={bg['top']}:c1={bg['bottom']}"
        f":x0={WIDTH // 2}:y0=0:x1={WIDTH // 2}:y1={HEIGHT}:nb_colors=2:r=30:d={duration:.3f}"
    )
    inputs = ["-f", "lavfi", "-i", gradient, "-i", str(clip)]
    inputs += ["-loop", "1", "-i", str(brand["overlay_path"])]
    if not audio:
        inputs += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    fit = (
        f"[1:v]scale={box['width']}:{box['height']}:force_original_aspect_ratio=decrease"
        f":flags=lanczos,pad={box['width']}:{box['height']}:(ow-iw)/2:(oh-ih)/2"
        f":color={bg['bottom']},setsar=1[clip]"
    )
    graph = (
        f"{fit};[0:v][clip]overlay={box['x']}:{box['y']}:shortest=1[base];"
        "[base][2:v]overlay=0:0:shortest=1,format=yuv420p[video]"
    )
    sound = ["-map", "1:a:0", "-af", "loudnorm=I=-18:TP=-1.5:LRA=11"] if audio else ["-map", "3:a"]
    return [
        ffmpeg, "-v", "error", "-nostdin", "-y", *inputs, "-filter_complex", graph,
        "-map", "[video]", *sound, "-t", f"{duration:.3f}", *ENCODE, "-f", "mp4", str(out),
    ]  # fmt: skip


def compose(
    clip: str | Path,
    out: str | Path,
    *,
    ffmpeg: str = "/usr/bin/ffmpeg",
    ffprobe: str = "/usr/bin/ffprobe",
    brand_root: Path = BRAND,
    timeout: float = 900,
) -> dict[str, Any]:
    clip, out = Path(clip), Path(out)
    if out.suffix != ".mp4" or out.exists() or out.is_symlink():
        raise ValueError("Output must be a new .mp4 file")
    if clip.is_symlink() or not clip.is_file():
        raise ValueError("Clip must be a regular file")
    brand = load_brand(brand_root)
    source = probe(ffprobe, clip)
    if not 0 < source["duration"] <= MAX_SECONDS:
        raise ValueError("Clip duration out of range")
    partial = out.with_name(out.name + ".partial")
    if partial.exists() or partial.is_symlink():
        raise ValueError("Partial output already exists")
    try:
        run(command(ffmpeg, clip, partial, brand, source["duration"], source["audio"]), timeout)
        result = probe(ffprobe, partial)
        if (
            (result["width"], result["height"]) != (WIDTH, HEIGHT)
            or result["codec"] != "h264"
            or result["audio_codec"] != "aac"
            or result["sample_rate"] != 48000
            or abs(result["duration"] - source["duration"]) > 0.25
        ):
            raise ValueError("Framed video failed its checks")
        os.replace(partial, out)
    finally:
        partial.unlink(missing_ok=True)
    return {
        "output": str(out),
        "video_sha256": sha256(out),
        "duration": result["duration"],
        "source": {k: source[k] for k in ("width", "height", "duration", "audio")},
        "overlay_sha256": brand["overlay"]["sha256"],
        "external_delivery": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clip", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(compose(args.clip, args.out), ensure_ascii=False))
    except Exception as error:
        print("BRAND_FRAME_BLOCKED: " + type(error).__name__ + ": " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
