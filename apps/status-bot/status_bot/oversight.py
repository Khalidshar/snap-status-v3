"""The six Status local tools (outside the per-job chain), same roles as Snap's.

manager plans, analyst counts, writer drafts the bulletin text, reviewer reports what
the chain would refuse, fixer repairs spacing and punctuation only. None of them
renders, approves, schedules or sends. content_repair is studio_core.repair.maintenance.
"""

import re
import sqlite3
from typing import Any

from .agents import (
    ARABIC_PUNCTUATION,
    CheckRejected,
    check_rotation,
    proofread,
    translation_requirement,
)

ACTIVE = (
    "QUEUED",
    "RESEARCHING",
    "PROCESSING",
    "DOWNLOADING",
    "DESIGNING",
    "RENDERING",
    "VALIDATING",
    "DELIVERING",
)


def plan(db: sqlite3.Connection) -> dict[str, Any]:
    """مدير المحتوى: one weather job at a time; never enqueues anything itself."""
    states = db.execute("SELECT state FROM jobs").fetchall()
    busy = sum(1 for row in states if row[0] in ACTIVE)
    if busy:
        return {"decision": "HOLD", "reason": "JOB_ACTIVE", "active": busy}
    return {"decision": "READY", "command": "weather"}


def analyse(db: sqlite3.Connection) -> dict[str, Any]:
    """محلل الأداء: outcomes per role and the causes of failures. Reads only."""
    rows = db.execute(
        "SELECT role,status,error_code FROM agent_runs WHERE app='status-bot-v2'"
    ).fetchall()
    outcomes: dict[str, dict[str, int]] = {}
    causes: dict[str, int] = {}
    for row in rows:
        per = outcomes.setdefault(row["role"], {})
        per[row["status"]] = per.get(row["status"], 0) + 1
        if row["error_code"]:
            causes[row["error_code"]] = causes.get(row["error_code"], 0) + 1
    return {"runs": len(rows), "outcomes": outcomes, "causes": causes}


def write(temperature_c: float, wind_kmh: float) -> str:
    """كاتب المحتوى: the bulletin body in the exact wording the source agent produces."""
    return f"الحرارة {temperature_c:.1f} درجة مئوية\nالرياح {wind_kmh:.1f} كم في الساعة"


def review(content: Any, city: str) -> dict[str, Any]:
    """المراجع: every reason the chain would refuse this content, without raising."""
    problems = []
    for check in (
        lambda: check_rotation(content, city),
        lambda: [proofread(content[key]) for key in ("title", "body")],
        lambda: translation_requirement(content),
    ):
        try:
            check()
        except CheckRejected as error:
            problems.append(str(error))
    return {"decision": "PASS" if not problems else "REJECT", "problems": problems}


def fix(text: str) -> dict[str, Any]:
    """مصلح المسودة: spacing, tatweel and punctuation spacing only; the words never change."""
    lines = []
    for line in text.split("\n"):
        line = line.replace("ـ", "")
        line = re.sub(r"\s+([" + re.escape(ARABIC_PUNCTUATION) + "])", r"\1", line)
        line = re.sub(r"([" + re.escape(ARABIC_PUNCTUATION) + r"])\1+", r"\1", line)
        lines.append(" ".join(line.split()))
    fixed = "\n".join(lines)
    if words(fixed) != words(text):
        raise CheckRejected("FIXER_WOULD_CHANGE_WORDS")
    return {"text": fixed, "changed": fixed != text}


def words(text: str) -> list[str]:
    return re.findall(r"[^\s" + re.escape(ARABIC_PUNCTUATION) + "]+", text.replace("ـ", ""))
