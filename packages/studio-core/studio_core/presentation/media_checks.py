"""Shared post-render byte, profile and audio checks; no app authority."""

import hashlib
import json
import math
import os
import re
import stat
import struct
from pathlib import Path
from typing import Any

from .content_agents import AgentRejected
from .sacred_cards import duration as sacred_duration

MAX_VIDEO = 50_000_000


def verified_bytes(path: str | Path, expected: str) -> bytes:
    """Consume the SAME verified byte buffer; do not reopen source for upload."""
    if not re.fullmatch("[0-9a-f]{64}", expected):
        raise ValueError("Invalid SHA")
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Symlink source")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_VIDEO:
            raise ValueError("Invalid video size/type")
        data = source.read(MAX_VIDEO + 1)
        after = os.fstat(source.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("Source changed while consuming")
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Video hash mismatch")
    return data


# The same limits render.validate enforces; the voice agent holds them independently.
LOUDNESS_LUFS = (-19.0, -17.0)
MAX_TRUE_PEAK_DBTP = -1.5
MIN_PHONE_LUFS = -24.0
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check_voice(result: Any) -> dict[str, Any]:
    """الصوت: one generated ambient bed, measured loud enough and never clipping."""
    if result.get("legacy_production"):
        from studio_core.production.pipeline import check_result

        check_result(result)
        audio = result["qa"]["audio_qa"]
        if result["qa"]["audio"] == "silent_sacred":
            if audio.get("max_volume_db", 0) > -80:
                raise AgentRejected("VOICE_SACRED_NOT_SILENT")
        elif (
            not -22 <= audio.get("integrated_lufs", 0) <= -8
            or audio.get("true_peak_dbtp", 0) > -0.5
        ):
            raise AgentRejected("VOICE_LEGACY_MEASUREMENT_INVALID")
        return {"decision": "PASS", "audio": result["qa"]["audio"]}
    if result.get("quran_foundation"):
        from studio_core.religious.quran_pipeline import check_result

        check_result(result)
        return {"decision": "PASS", "audio": result["qa"]["audio"]}
    qa = result["qa"]
    if qa.get("audio") == "silent_sacred":
        # Religious clips: no tone or music (owner decision 2 Nov), verified silent.
        silence = qa.get("audio_qa")
        if (
            not isinstance(silence, dict)
            or set(silence) != {"max_volume_db"}
            or not _number(silence["max_volume_db"])
            or silence["max_volume_db"] > -80
        ):
            raise AgentRejected("VOICE_SACRED_NOT_SILENT")
        return {"decision": "PASS", "audio": "silent_sacred", **silence}
    if qa.get("audio") != "generated_ambient":
        raise AgentRejected("VOICE_AUDIO_NOT_AMBIENT")
    audio = qa.get("audio_qa")
    if not isinstance(audio, dict) or set(audio) != {
        "integrated_lufs",
        "true_peak_dbtp",
        "phone_lufs",
    }:
        raise AgentRejected("VOICE_MEASUREMENT_MISSING")
    if not all(_number(value) for value in audio.values()):
        raise AgentRejected("VOICE_MEASUREMENT_INVALID")
    if not LOUDNESS_LUFS[0] <= audio["integrated_lufs"] <= LOUDNESS_LUFS[1]:
        raise AgentRejected("VOICE_LOUDNESS_OUT_OF_RANGE")
    if audio["true_peak_dbtp"] > MAX_TRUE_PEAK_DBTP:
        raise AgentRejected("VOICE_PEAK_TOO_HIGH")
    if audio["phone_lufs"] < MIN_PHONE_LUFS:
        raise AgentRejected("VOICE_TOO_QUIET_ON_PHONE")
    return {"decision": "PASS", "audio": "generated_ambient", **audio}


def mp4_boxes(data: bytes) -> list[str]:
    """Top-level ISO BMFF box types, in order; malformed sizes are rejected."""
    boxes: list[str] = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise AgentRejected("MEDIA_CONTAINER_INVALID")
        size, kind = struct.unpack(">I4s", data[offset : offset + 8])
        header = 8
        if size == 1:
            if len(data) - offset < 16:
                raise AgentRejected("MEDIA_CONTAINER_INVALID")
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
            header = 16
        elif size == 0:
            size = len(data) - offset
        if size < header or offset + size > len(data):
            raise AgentRejected("MEDIA_CONTAINER_INVALID")
        boxes.append(kind.decode("latin-1"))
        offset += size
    return boxes


def _under_root(root: Path, path: Path) -> None:
    """No symlink anywhere from the spool root down to the file."""
    if root not in path.parents:
        raise AgentRejected("MEDIA_PATH_UNSAFE")
    item = path
    while True:
        if item.is_symlink():
            raise AgentRejected("MEDIA_PATH_UNSAFE")
        if item == root:
            return
        item = item.parent


def check_media(result: Any, settings: Any) -> dict[str, Any]:
    """التحقق من الوسائط: the adopted-to-be bytes are exactly what the result declares."""
    profile = settings.get("profile") or {}
    root = Path(settings["spool_root"])
    if not root.is_absolute() or root.name != "renders":
        raise AgentRejected("MEDIA_PATH_UNSAFE")
    video = root.parent / result["artifact"]
    _under_root(root, video)
    try:
        data = verified_bytes(video, result["video_sha256"])
    except (OSError, ValueError) as cause:
        raise AgentRejected("MEDIA_DIGEST_MISMATCH") from cause
    qa = result["qa"]
    try:
        if result.get("legacy_production"):
            from studio_core.production.pipeline import check_result

            expected_duration = check_result(result)
        elif result.get("quran_foundation"):
            from studio_core.religious.quran_pipeline import check_result

            expected_duration = check_result(result)
        else:
            expected_duration = sacred_duration(result, profile.get("duration_seconds", 0))
    except (ValueError, TypeError, KeyError) as cause:
        raise AgentRejected("MEDIA_SACRED_PAGES_CHANGED") from cause
    if qa.get("bytes") != len(data) or len(data) > profile.get("max_bytes", 0):
        raise AgentRejected("MEDIA_SIZE_MISMATCH")
    if (
        (qa.get("width"), qa.get("height"), qa.get("codec"))
        != (profile.get("width"), profile.get("height"), "h264")
        or not _number(qa.get("duration"))
        or not _number(profile.get("duration_seconds"))
        or abs(qa["duration"] - expected_duration) > 0.25
    ):
        raise AgentRejected("MEDIA_PROFILE_MISMATCH")
    boxes = mp4_boxes(data)
    if not boxes or boxes[0] != "ftyp" or "moov" not in boxes or "mdat" not in boxes:
        raise AgentRejected("MEDIA_CONTAINER_INVALID")
    if boxes.index("moov") > boxes.index("mdat"):
        raise AgentRejected("MEDIA_NOT_FASTSTART")
    record = video.parent / "render-result.json"
    _under_root(root, record)
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2).encode()
    if not record.is_file() or record.read_bytes() != encoded:
        raise AgentRejected("MEDIA_RESULT_RECORD_MISMATCH")
    preview = video.parent / "preview.png"
    _under_root(root, preview)
    if not preview.is_file() or preview.read_bytes()[:8] != PNG_SIGNATURE:
        raise AgentRejected("MEDIA_PREVIEW_MISSING")
    if (result.get("font_sha256"), result.get("latin_font_sha256")) != (
        profile.get("font_sha256"),
        profile.get("latin_font_sha256"),
    ):
        raise AgentRejected("MEDIA_FONT_PROVENANCE_MISMATCH")
    if (
        result.get("glyph_coverage") != "PASS"
        or (result.get("safe_area") or {}).get("result") != "PASS"
    ):
        raise AgentRejected("MEDIA_LAYOUT_UNVERIFIED")
    return {"decision": "PASS", "bytes": len(data), "boxes": boxes}
