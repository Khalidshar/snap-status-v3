"""Shared pieces of «أدوات الوكلاء» and «أضف معلومة» for both bots (owner 21 Nov 2026).

Each bot reads its own evidence (Snap: preview event log; WhatsApp: command queue) and
uses these helpers so both answer the same way. Read-only, except the owner's fact queue,
which only appends the owner's own text for review: a fact enters «هل تعلم» only after its
source is verified, like every catalog fact.
"""

import contextlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from studio_core.commands import BY_KEY

from . import chain_gate, role_status

NAMES = {
    "manager": "مدير المحتوى",
    "analyst": "محلل الأداء",
    "writer": "كاتب المحتوى",
    "reviewer": "مراجع المحتوى",
    "fixer": "مصلح المسودة",
    "content_repair": "إصلاح المحتوى",
}
DAY = 86400
SACRED = {
    "adhkar_am",
    "adhkar_pm",
    "dua",
    "hadith",
    "verse",
    "friday",
    "kahf",
    "fast",
    "isha",
    "night",
}
FACTS = "owner-facts.jsonl"
FACT_LIMITS = (10, 400)


def ar(n: int) -> str:
    return str(n).translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))


def label(kind: str) -> str:
    command = BY_KEY.get("facts" if kind == "did_you_know" else kind)
    return command.label if command else kind


def kind_of(value: dict[str, Any]) -> str:
    plan = value.get("legacy_production") or {}
    return str(plan.get("key") or value.get("kind") or "did_you_know")


def heads(value: dict[str, Any]) -> list[str]:
    plan = value.get("legacy_production") or {}
    return [str(s["head"]) for s in plan.get("scenes", []) if s.get("head")]


def writer(value: dict[str, Any]) -> str:
    body = heads(value) or [str(value.get("body", ""))[:200]]
    return "آخر نص: " + label(kind_of(value)) + "\n" + "\n".join("• " + h for h in body[:6])


def reviewer(ledger: Path, value: dict[str, Any]) -> str:
    try:
        chain_gate.verify(ledger)
        verdict = "✅ مرّ على الوكلاء الـ١٨ بالترتيب المعتمد."
    except ValueError as error:
        verdict = (
            "❌ " + ("سجل الوكلاء ناقص: " if "INCOMPLETE" in str(error) else "") + str(error)[:160]
        )
    return "آخر مقطع: " + label(kind_of(value)) + "\n" + verdict


def fixer(value: dict[str, Any]) -> str:
    if kind_of(value) in SACRED or value.get("sacred"):
        return "آخر مقطع نص ديني مقفل؛ لا يُقترح عليه أي تعديل."
    plan = value.get("legacy_production") or {}
    notes = []
    for index, scene in enumerate(plan.get("scenes", []), 1):
        head, body = str(scene.get("head") or ""), str(scene.get("body") or "")
        if len(head) > 80:
            notes.append(f"• المشهد {ar(index)}: العنوان طويل ({ar(len(head))} حرف).")
        if body.rstrip().endswith(("…", "...")):
            notes.append(f"• المشهد {ar(index)}: الملخص مقطوع بـ«…».")
        if scene.get("image") and not scene.get("image_credit"):
            notes.append(f"• المشهد {ar(index)}: الصورة بلا مصدر.")
    if not notes:
        return "ما لقيت ملاحظات على آخر مسودة. (اقتراحات فقط؛ ما عدّلت شي)"
    return "ملاحظات على آخر مسودة (اقتراحات فقط؛ ما عدّلت شي):\n" + "\n".join(notes[:8])


def status_lines(root: Path) -> list[str]:
    """Halt, schedule and the owner's fact queue, for «مدير المحتوى»."""
    lines = []
    if (root / "HALT").exists() or (root / "commands" / "HALT").exists():
        lines.append("🛑 الإرسال موقوف بالكامل («إيقاف كامل»).")
    paused = (root / "SCHEDULE_PAUSED").exists()
    lines.append("🗓 الجدولة: " + ("⏸ موقوفة" if paused else "▶️ شغالة"))
    queued = len(queued_facts(root))
    if queued:
        lines.append(f"📥 معلومات تنتظر التحقق من مصدرها: {ar(queued)}")
    return lines


def run(app: str, key: str, ledger: Path, work: Callable[[], str]) -> str:
    """One tool, with its RUNNING and outcome recorded in the bot's agent-status ledger."""
    if key not in NAMES:
        raise ValueError("UNKNOWN_TOOL")
    with contextlib.suppress(OSError, ValueError):
        role_status.record(ledger, app, key, "RUNNING")
    try:
        text, state = work(), "SUCCEEDED"
    except Exception:
        text, state = "تعذر إكمال الأداة؛ ما فيه مقطع صالح أقرأه بعد.", "FAILED"
    with contextlib.suppress(OSError, ValueError):
        role_status.record(ledger, app, key, state)
    return "🤖 " + NAMES[key] + "\n" + text


def queued_facts(root: Path) -> list[dict[str, Any]]:
    path = root / FACTS
    if not path.is_file():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):
            item = json.loads(line)
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                items.append(item)
    return items


def add_fact(root: Path, text: str, now: float | None = None) -> str:
    """«أضف هل تعلم: …» — the owner's fact joins a review queue; nothing is published."""
    text = " ".join(text.split())
    low, high = FACT_LIMITS
    if not low <= len(text) <= high:
        return f"✍️ المعلومة لازم تكون بين {ar(low)} و{ar(high)} حرف. ما انحفظ شي."
    if any(item["text"] == text for item in queued_facts(root)):
        return "✍️ هذي المعلومة محفوظة من قبل في قائمة المراجعة."
    record = {"text": text, "at": time.time() if now is None else now}
    with (root / FACTS).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    count = len(queued_facts(root))
    return (
        f"📥 انحفظت المعلومة في قائمة المراجعة (رقم {ar(count)}).\n"
        "ما تدخل «هل تعلم» إلا بعد التحقق من مصدرها، مثل كل معلومات الكتالوج."
    )


FACT_HELP = "✍️ أضف معلومة: اكتب «أضف هل تعلم: » وبعدها المعلومة في نفس الرسالة."
