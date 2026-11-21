"""«أدوات الوكلاء» in the WhatsApp bot, on WhatsApp's own evidence (owner 21 Nov 2026).

The six tools used to run the old weather helpers (a weather sentence, a review for Riyadh).
They now read the WhatsApp command queue, the clips' content and their agent ledgers, and
answer like the Snap bot (shared wording in studio_core.observability.agent_tools).
Read-only: nothing is rendered, sent, edited or retried, and sacred text is never touched.
"""

import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

from studio_core.observability import agent_tools as shared
from studio_core.observability import role_status
from studio_core.observability.agent_registry import status_text
from studio_core.observability.agent_tools import DAY, NAMES, ar, label

from .server_preview import STATE

__all__ = ["NAMES", "execute", "ledger", "status"]
# Expected outcomes, not faults: no fresh story, nothing new in fashion.
BENIGN = ("NO_FRESH", "FASHION_UNAVAILABLE")


def ledger(outbox: Any) -> Path:
    return Path(outbox.repo.config.data_root) / "agent-status.sqlite"


def status(outbox: Any) -> str:
    rows = role_status.snapshot(ledger(outbox), "status-bot-v2")
    return status_text("status-bot-v2", {r["id"]: r for r in rows})


def clips(outbox: Any) -> list[dict[str, Any]]:
    """The owner's WhatsApp clip requests, oldest first."""
    with outbox.repo.connect() as db:
        try:
            cursor = db.execute(
                "SELECT tag, kind, state, at, code, content_json FROM command_videos"
                " WHERE binding=? ORDER BY at",
                (outbox.identity.binding,),
            )
        except sqlite3.OperationalError:
            return []
        names = [c[0] for c in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def latest(outbox: Any) -> dict[str, Any]:
    for row in reversed(clips(outbox)):
        if row.get("content_json"):
            value = dict(json.loads(row["content_json"]))
            value.setdefault("_tag", row["tag"])
            return value
    raise ValueError("NO_CONTENT")


def failed(row: dict[str, Any]) -> bool:
    state, code = str(row.get("state") or ""), str(row.get("code") or "")
    return ("BLOCK" in state or "FAIL" in state) and not any(b in code for b in BENIGN)


def manager(outbox: Any) -> str:
    working = [r for r in clips(outbox) if r.get("state") == "WORKING"]
    first = (
        "⏳ فيه مقطع قيد الصنع: " + label(str(working[-1]["kind"]))
        if working
        else "✅ جاهز لتلقي الأوامر؛ ما فيه مقطع قيد الصنع."
    )
    return "\n".join([first, *shared.status_lines(STATE)])


def analyst(outbox: Any, now: float) -> str:
    rows = clips(outbox)

    def made(since: float) -> list[dict[str, Any]]:
        return [r for r in rows if float(r.get("at") or 0) >= since]

    def sent(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [r for r in items if r.get("state") == "API_ACCEPTED"]

    today, week = made(now - DAY), made(now - 7 * DAY)
    kinds = Counter(label(str(r["kind"])) for r in sent(week))
    lines = [
        f"اليوم: طُلب {ar(len(today))} · أُرسل {ar(len(sent(today)))}"
        f" · فشل {ar(sum(failed(r) for r in today))}",
        f"آخر ٧ أيام: طُلب {ar(len(week))} · أُرسل {ar(len(sent(week)))}",
    ]
    if kinds:
        lines.append("المرسَل حسب النوع (٧ أيام):")
        lines += [f"• {name}: {ar(n)}" for name, n in kinds.most_common(10)]
    return "\n".join(lines)


def reviewer(outbox: Any) -> str:
    value = latest(outbox)
    folder = STATE / "content-clips" / str(value.get("_tag", ""))
    found = sorted(folder.glob("**/jobs.sqlite")) if value.get("_tag") else []
    ledger_path = found[0] if found else folder / "jobs.sqlite"
    return shared.reviewer(ledger_path, value)


def content_repair(outbox: Any, now: float) -> str:
    bad = [r for r in clips(outbox) if float(r.get("at") or 0) >= now - 7 * DAY and failed(r)]
    if not bad:
        return "ما فيه مقطع واتساب فشل خلال ٧ أيام. لم يُعد إرسال أي مقطع."
    lines = [f"فشل {ar(len(bad))} مقطع خلال ٧ أيام (لم يُعد إرسال أي مقطع):"]
    for row in bad[-6:]:
        when = time.strftime("%m-%d %H:%M", time.localtime(float(row.get("at") or 0)))
        code = str(row.get("code") or row.get("state") or "")
        lines.append(f"• {when} {label(str(row['kind']))}" + (f" — {code}" if code else ""))
    return "\n".join(lines)


def execute(key: str, outbox: Any) -> str:
    now = time.time()

    def work() -> str:
        if key == "manager":
            return manager(outbox)
        if key == "analyst":
            return analyst(outbox, now)
        if key == "content_repair":
            return content_repair(outbox, now)
        if key == "reviewer":
            return reviewer(outbox)
        value = latest(outbox)
        return shared.writer(value) if key == "writer" else shared.fixer(value)

    return shared.run("status-bot-v2", key, ledger(outbox), work)
