"""Owner-requested local tools. No rendering, network, approval or replay."""

import json
import sqlite3
from pathlib import Path
from typing import Any

from studio_core.observability import role_status
from studio_core.observability.agent_registry import status_text
from studio_core.repair.maintenance import diagnose

from . import oversight
from .agents import LOCAL_TOOLS

NAMES = {
    "manager": "مدير المحتوى",
    "analyst": "محلل الأداء",
    "writer": "كاتب المحتوى",
    "reviewer": "مراجع المحتوى",
    "fixer": "مصلح المسودة",
    "content_repair": "إصلاح المحتوى",
}
STATES = {"UNKNOWN": "غير معروف", "RUNNING": "يعمل الآن", "SUCCEEDED": "نجح", "FAILED": "فشل"}


def ledger(outbox: Any) -> Path:
    return Path(outbox.repo.config.data_root) / "agent-status.sqlite"


def status(outbox: Any) -> str:
    rows = role_status.snapshot(ledger(outbox), "status-bot-v2")
    return status_text("status-bot-v2", {r["id"]: r for r in rows})



def latest(outbox: Any) -> dict[str, Any]:
    with outbox.repo.connect() as db:
        try:
            row = db.execute(
                "SELECT content_json FROM command_videos WHERE binding=? AND content_json IS NOT NULL ORDER BY at DESC LIMIT 1",
                (outbox.identity.binding,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
    if row is None:
        raise ValueError("NO_CONTENT")
    return dict(json.loads(row[0]))


def execute(key: str, outbox: Any) -> str:
    if key not in LOCAL_TOOLS:
        raise ValueError("UNKNOWN_TOOL")
    path = ledger(outbox)
    role_status.record(path, "status-bot-v2", key, "RUNNING")
    try:
        if key in {"manager", "analyst"}:
            with outbox.repo.connect() as db:
                value = oversight.plan(db) if key == "manager" else {"runs": 0}
            if key == "analyst":
                with sqlite3.connect(path) as db:
                    value["runs"] = db.execute(
                        "SELECT count(*) FROM runs WHERE state IN ('SUCCEEDED','FAILED')"
                    ).fetchone()[0]
            text = (
                (
                    "جاهز لتلقي الأوامر"
                    if value.get("decision") == "READY"
                    else "توجد مهمة قيد التنفيذ"
                )
                if key == "manager"
                else "عدد تشغيلات الوكلاء: " + str(value["runs"])
            )
        elif key == "content_repair":
            value = diagnose(outbox.repo)
            items: Any = value.get("items", [])
            count = len(items) if isinstance(items, list) else 0
            text = "اكتمل تشخيص فرص إصلاح المحتوى؛ لم يُعد إرسال أي مقطع.\n" + str(count)
        else:
            content = latest(outbox)
            if key == "writer":
                text = oversight.write(content)
            elif key == "reviewer":
                value = oversight.review(content, "الرياض")
                text = "المراجعة: " + ("اجتاز" if value["decision"] == "PASS" else "لم يجتز")
            else:
                if content.get("sacred"):
                    raise ValueError("SACRED_TEXT_LOCKED")
                text = oversight.fix(oversight.write(content))["text"]
        role_status.record(path, "status-bot-v2", key, "SUCCEEDED")
        return NAMES[key] + "\n" + str(text)
    except Exception:
        role_status.record(path, "status-bot-v2", key, "FAILED")
        return NAMES[key] + "\nتعذر إكمال الأداة؛ يلزم محتوى صالح، ولا يُعدّل النص الديني المقفل."
