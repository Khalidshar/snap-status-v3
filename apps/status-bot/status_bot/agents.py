"""Owned Status weather adapters. Local rules only, never publication authority.

The eighteen roles in the same order as Snap (snap_bot.agent_contract_map): twelve run
in every weather job's chain, six are local tools outside it (status_bot.oversight).
Check agents verify their input and pass it through unchanged; none rewrites, renders,
adopts or delivers.
"""

import math
import re
import sqlite3
import struct
import time
from typing import Any

from studio_core.observability.agent_runs import AgentContract, AgentRejected, digest

from .weather import content_hash

# Same chain and order as Snap; the twelfth, final, is the gate after quality.
CHAIN = (
    "fact_source",
    "arabic",
    "memory",
    "rotation",
    "proofreading",
    "translation",
    "design",
    "voice",
    "media_verification",
    "judge",
    "quality",
    "final",
)
CONTENT_CHECKS = ("memory", "rotation", "proofreading", "translation")
RENDER_CHECKS = ("voice", "media_verification", "judge")
LOCAL_TOOLS = {
    "manager": "oversight.plan",
    "analyst": "oversight.analyse",
    "writer": "oversight.write",
    "reviewer": "oversight.review",
    "fixer": "oversight.fix",
    "content_repair": "studio_core.repair.maintenance",
}
ROLES = (*CHAIN, *LOCAL_TOOLS)
DEPENDENCIES = {role: (CHAIN[i - 1],) if i else () for i, role in enumerate(CHAIN)}
ARABIC_PUNCTUATION = "،؛؟.:!"
SILENT = "not_required_for_weather_card"

PLAN_SHA = digest(
    {
        "app": "status-bot-v2",
        "command": "weather",
        "version": "2.0.0",
        "roles": {role: list(DEPENDENCIES[role]) for role in CHAIN},
        "local_tools": LOCAL_TOOLS,
        "source": "weather-v1",
        "arabic": "weather-labels-v1",
        "checks": "weather-pass-through-v1",
        "quality": "owned-content-and-local-video-hash-v1",
    }
)


class CheckRejected(AgentRejected):
    """A check agent refused the weather job; nothing is rendered, adopted or sent."""


def contract(role: str, budget: int = 25) -> AgentContract:
    if role not in DEPENDENCIES:
        raise ValueError("Unknown Status agent role")
    return AgentContract(
        "status-bot-v2", "weather", role, "2.0.0", PLAN_SHA, DEPENDENCIES[role], budget
    )


def check_memory(db: sqlite3.Connection, job: str, payload: Any) -> dict[str, Any]:
    """مراجع التكرار: no other job already produced this exact weather payload."""
    repeat = db.execute(
        "SELECT 1 FROM agent_runs WHERE app='status-bot-v2' AND role='arabic' AND status='SUCCEEDED'"
        " AND job_id<>? AND output_sha=? LIMIT 1",
        (job, digest(payload)),
    ).fetchone()
    if repeat:
        raise CheckRejected("MEMORY_REPEAT")
    return {"decision": "PASS"}


def check_rotation(content: Any, city: str) -> dict[str, Any]:
    """التناوب: the bulletin is for the scheduled city (or the labelled synthetic fixture)."""
    expected = "تجربة الطقس" if content.get("source") == "synthetic-fixture" else "طقس " + city
    if content.get("title") != expected:
        raise CheckRejected("ROTATION_WRONG_CITY")
    return {"decision": "PASS", "city": city}


def proofread(text: str) -> dict[str, Any]:
    """التدقيق: spacing and punctuation hygiene, line by line; reports, never edits."""
    for line in text.split("\n"):
        if "  " in line or line != line.strip():
            raise CheckRejected("PROOFREADING_SPACING")
        if re.search(r"\s[" + re.escape(ARABIC_PUNCTUATION) + "]", line):
            raise CheckRejected("PROOFREADING_SPACE_BEFORE_PUNCTUATION")
        if re.search(r"([" + re.escape(ARABIC_PUNCTUATION) + r"])\1", line):
            raise CheckRejected("PROOFREADING_REPEATED_PUNCTUATION")
        if "ـ" in line:
            raise CheckRejected("PROOFREADING_TATWEEL")
    return {"decision": "PASS"}


def translation_requirement(content: Any) -> dict[str, Any]:
    """الترجمة: the title and body are Arabic; only the source attribution may be Latin."""
    for key in ("title", "body"):
        if re.search(r"[A-Za-z]", content[key]):
            raise CheckRejected("TRANSLATION_REQUIRED")
    return {"decision": "PASS", "required": False}


def run_content_check(role: str, db: sqlite3.Connection, job: str, payload: Any, city: str) -> Any:
    """One content check on the weather payload; returns it unchanged."""
    if role not in CONTENT_CHECKS or not weather_payload(payload):
        raise CheckRejected("CHECK_INPUT_INVALID")
    content = payload["content"]
    if role == "memory":
        check_memory(db, job, payload)
    elif role == "rotation":
        check_rotation(content, city)
    elif role == "proofreading":
        for key in ("title", "body"):
            proofread(content[key])
    else:
        translation_requirement(content)
    return payload


def mp4_boxes(data: bytes) -> list[str]:
    """Top-level ISO BMFF box types, in order; malformed sizes are rejected."""
    boxes: list[str] = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise CheckRejected("MEDIA_CONTAINER_INVALID")
        size, kind = struct.unpack(">I4s", data[offset : offset + 8])
        header = 8
        if size == 1:
            if len(data) - offset < 16:
                raise CheckRejected("MEDIA_CONTAINER_INVALID")
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
            header = 16
        elif size == 0:
            size = len(data) - offset
        if size < header or offset + size > len(data):
            raise CheckRejected("MEDIA_CONTAINER_INVALID")
        boxes.append(kind.decode("latin-1"))
        offset += size
    return boxes


def check_voice(result: Any) -> dict[str, Any]:
    """الصوت: the weather card is silent by design; any audio claim is refused."""
    if result["qa"].get("audio") != SILENT:
        raise CheckRejected("VOICE_WEATHER_NOT_SILENT")
    return {"decision": "PASS", "audio": SILENT}


def check_media(result: Any, data: bytes) -> dict[str, Any]:
    """التحقق من الوسائط: a fast-start MP4 whose bytes are what the result declares."""
    import hashlib

    if hashlib.sha256(data).hexdigest() != result["video_sha256"]:
        raise CheckRejected("MEDIA_DIGEST_MISMATCH")
    boxes = mp4_boxes(data)
    if not boxes or boxes[0] != "ftyp" or "moov" not in boxes or "mdat" not in boxes:
        raise CheckRejected("MEDIA_CONTAINER_INVALID")
    if boxes.index("moov") > boxes.index("mdat"):
        raise CheckRejected("MEDIA_NOT_FASTSTART")
    size = result["qa"].get("bytes")
    if size is not None and size != len(data):
        raise CheckRejected("MEDIA_SIZE_MISMATCH")
    return {"decision": "PASS", "bytes": len(data), "boxes": boxes}


def judge(db: sqlite3.Connection, job: str, attempt: int) -> dict[str, Any]:
    """الحَكَم: every earlier chain agent of this attempt succeeded, under this plan."""
    rows = {
        row["role"]: row
        for row in db.execute(
            "SELECT role,status,contract_sha FROM agent_runs WHERE app='status-bot-v2' AND job_id=?"
            " AND attempt=?",
            (job, attempt),
        )
    }
    earlier = CHAIN[: CHAIN.index("judge")]
    if any(role not in rows or rows[role]["status"] != "SUCCEEDED" for role in earlier):
        raise CheckRejected("JUDGE_EVIDENCE_INCOMPLETE")
    if any(rows[role]["contract_sha"] != PLAN_SHA for role in earlier):
        raise CheckRejected("JUDGE_PLAN_CHANGED")
    return {"decision": "PASS", "agents": len(earlier)}


def source_request(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"command", "synthetic"}
        and value["command"] == "weather"
        and type(value["synthetic"]) is bool
    )


def weather_payload(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"content", "failures"}:
        return False
    content = value["content"]
    if not isinstance(content, dict) or not isinstance(value["failures"], list):
        return False
    if (
        content.get("type") != "weather"
        or content.get("sacred") is not False
        or content.get("language") != "ar"
        or content.get("content_hash") != content_hash(content)
    ):
        return False
    for key, limit in (("title", 100), ("body", 1000), ("attribution", 110)):
        text = content.get(key)
        if (
            not isinstance(text, str)
            or not 1 <= len(text) <= limit
            or any(ord(char) < 32 and not (char == "\n" and key == "body") for char in text)
            or any(
                char in text
                for char in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200b\ufeff"
            )
        ):
            return False
    if not re.search("[\u0600-\u06ff]", content["title"]) or not re.search(
        "[\u0600-\u06ff]", content["body"]
    ):
        return False
    for key, low, high in (("temperature_c", -90, 65), ("wind_kmh", 0, 500)):
        number: Any = content.get(key)
        if (
            type(number) not in (int, float)
            or not math.isfinite(number)
            or not low <= number <= high
        ):
            return False
    expiry: Any = content.get("expires_at")
    return type(expiry) in (int, float) and math.isfinite(expiry) and expiry > time.time()


def source_output(value: Any, request: Any) -> bool:
    return weather_payload(value) and (
        not request["synthetic"] or value["content"]["source"] == "synthetic-fixture"
    )


def arabic_output(value: Any, original: Any) -> bool:
    # Check the weather labels without rewriting or asserting full proofreading.
    return weather_payload(value) and digest(value) == digest(original)
