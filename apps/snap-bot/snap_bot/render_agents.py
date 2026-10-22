"""Three deterministic post-render agents for did_you_know videos.

Each re-checks the renderer's result and the immutable spool bytes, then passes
the result through unchanged. No FFmpeg, AI, network, re-render, adoption or
delivery here: a rejection only stops the job before quality adopts the video.
"""

import json
import math
import struct
from pathlib import Path

from .agent_contract_map import (
    BOUND,
    RENDER_CHECKS,
    contract_plan,
)
from .content_agents import AgentRejected
from .n1_artifacts import verified_bytes
from .owned_contract_adapter import arabic_evidence, check_chain, digest

# The same limits render.validate enforces; the voice agent holds them independently.
LOUDNESS_LUFS = (-19.0, -17.0)
MAX_TRUE_PEAK_DBTP = -1.5
MIN_PHONE_LUFS = -24.0
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check_voice(result):
    """الصوت: one generated ambient bed, measured loud enough and never clipping."""
    qa = result["qa"]
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


def mp4_boxes(data):
    """Top-level ISO BMFF box types, in order; malformed sizes are rejected."""
    boxes, offset = [], 0
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


def _under_root(root, path):
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


def check_media(result, settings):
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
    if qa.get("bytes") != len(data) or len(data) > profile.get("max_bytes", 0):
        raise AgentRejected("MEDIA_SIZE_MISMATCH")
    if (
        (qa.get("width"), qa.get("height"), qa.get("codec"))
        != (profile.get("width"), profile.get("height"), "h264")
        or not _number(qa.get("duration"))
        or not _number(profile.get("duration_seconds"))
        or abs(qa["duration"] - profile["duration_seconds"]) > 0.25
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


def judge(db, job, attempt, result):
    """الحَكَم: every earlier agent of this attempt succeeded on the same lineage."""
    rows = {
        row["role"]: dict(row)
        for row in db.execute(
            "SELECT * FROM owned_agent_contracts WHERE job_id=? AND attempt=?", (job, attempt)
        )
    }
    earlier = BOUND[: BOUND.index("judge")]
    if any(role not in rows or rows[role]["status"] != "SUCCEEDED" for role in earlier):
        raise AgentRejected("JUDGE_EVIDENCE_INCOMPLETE")
    plan = contract_plan()["sha256"]
    if any(rows[role]["plan_sha"] != plan for role in earlier):
        raise AgentRejected("JUDGE_PLAN_CHANGED")
    try:
        arabic_evidence(rows["arabic"], rows["fact_source"]["output_sha"], result["content_hash"])
        check_chain(rows)
    except ValueError as cause:
        raise AgentRejected("JUDGE_CONTENT_LINEAGE_INVALID") from cause
    result_sha = digest(result)
    if (
        rows["design"]["input_sha"] != rows["fact_source"]["output_sha"]
        or rows["design"]["output_sha"] != result_sha
    ):
        raise AgentRejected("JUDGE_RENDER_LINEAGE_INVALID")
    for role in ("voice", "media_verification"):
        if rows[role]["input_sha"] != result_sha or rows[role]["output_sha"] != result_sha:
            raise AgentRejected("JUDGE_RENDER_LINEAGE_INVALID")
    return {"decision": "PASS", "agents": len(earlier)}


def run_render_check(role, repo, job, token, attempt, result, settings):
    """Run one post-render agent under the current lease; return the result unchanged."""
    if role not in RENDER_CHECKS:
        raise ValueError("Unknown render agent")
    if role == "voice":
        check_voice(result)
    elif role == "media_verification":
        check_media(result, settings)
    else:
        with repo.owned_transaction(job, token, attempt=attempt) as db:
            judge(db, job, attempt, result)
    return result
