"""«أدوات الوكلاء» in the Snap bot (owner 21 Nov 2026: the six tools answered «غير مربوطة»).

Each tool reads Snap's own evidence — the preview event log, the clips' content and their
agent ledgers — and answers in one message. Read-only: nothing is rendered, sent, approved,
edited or retried, and sacred text is never touched.
"""

import contextlib
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from studio_core.commands import BY_KEY
from studio_core.observability import chain_gate, role_status

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


def ar(n: int) -> str:
    return str(n).translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))


def label(kind: str) -> str:
    command = BY_KEY.get("facts" if kind == "did_you_know" else kind)
    return command.label if command else kind


def content(root: Path, preview: str) -> dict[str, Any]:
    for path in sorted(
        (root / "previews" / preview / "state").glob("renders/*/input/content.json")
    ):
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    return {}


def rendered(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("kind") == "RENDERED" and e.get("preview_id")]


def latest(root: Path, events: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    for event in reversed(rendered(events)):
        value = content(root, str(event["preview_id"]))
        if value:
            return event, value
    raise ValueError("NO_CONTENT")


def kind_of(value: dict[str, Any]) -> str:
    plan = value.get("legacy_production") or {}
    return str(plan.get("key") or value.get("kind") or "did_you_know")


def heads(value: dict[str, Any]) -> list[str]:
    plan = value.get("legacy_production") or {}
    return [str(s["head"]) for s in plan.get("scenes", []) if s.get("head")]


def manager(root: Path, events: list[dict[str, Any]], pending: Any) -> str:
    lines = []
    if (root / "HALT").exists():
        lines.append("🛑 الإرسال موقوف بالكامل («إيقاف كامل»).")
    if pending:
        lines.append("⏳ فيه مقطع ينتظر موافقتك: " + str(pending.get("title", "")))
    else:
        lines.append("✅ جاهز لتلقي الأوامر؛ ما فيه مقطع ينتظر.")
    paused = (root / "SCHEDULE_PAUSED").exists()
    lines.append("🗓 الجدولة: " + ("⏸ موقوفة" if paused else "▶️ شغالة"))
    return "\n".join(lines)


def analyst(root: Path, events: list[dict[str, Any]], now: float) -> str:
    def count(kind: str, since: float) -> int:
        return sum(1 for e in events if e.get("kind") == kind and float(e.get("time", 0)) >= since)

    blocked = ("PRE_NETWORK_BLOCKED", "UNKNOWN")
    lines = [
        f"اليوم: صُنع {ar(count('RENDERED', now - DAY))} · أُرسل {ar(count('ACK', now - DAY))}"
        f" · انمنع {ar(sum(count(k, now - DAY) for k in blocked))}",
        f"آخر ٧ أيام: صُنع {ar(count('RENDERED', now - 7 * DAY))} · أُرسل"
        f" {ar(count('ACK', now - 7 * DAY))}",
    ]
    sent = {e.get("video_sha") for e in events if e.get("kind") == "ACK"}
    kinds: Counter[str] = Counter()
    for event in rendered(events):
        if float(event.get("time", 0)) >= now - 7 * DAY and event.get("video_sha") in sent:
            kinds[label(kind_of(content(root, str(event["preview_id"]))))] += 1
    if kinds:
        lines.append("المرسَل حسب النوع (٧ أيام):")
        lines += [f"• {name}: {ar(n)}" for name, n in kinds.most_common(10)]
    return "\n".join(lines)


def writer(value: dict[str, Any]) -> str:
    body = heads(value) or [str(value.get("body", ""))[:200]]
    return "آخر نص: " + label(kind_of(value)) + "\n" + "\n".join("• " + h for h in body[:6])


def reviewer(root: Path, event: dict[str, Any], value: dict[str, Any]) -> str:
    ledger = root / "previews" / str(event["preview_id"]) / "state" / "jobs.sqlite"
    try:
        chain_gate.verify(ledger)
        verdict = "✅ مرّ على الوكلاء الـ١٨ بالترتيب المعتمد."
    except ValueError as error:
        verdict = (
            "❌ " + ("سجل الوكلاء ناقص: " if "INCOMPLETE" in str(error) else "") + str(error)[:160]
        )
    return "آخر مقطع: " + label(kind_of(value)) + "\n" + verdict


def fixer(value: dict[str, Any]) -> str:
    if kind_of(value) in SACRED:
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


def content_repair(root: Path, events: list[dict[str, Any]], now: float) -> str:
    blocked = [
        e
        for e in events
        if e.get("kind") in ("PRE_NETWORK_BLOCKED", "UNKNOWN")
        and float(e.get("time", 0)) >= now - 7 * DAY
    ]
    if not blocked:
        return "ما فيه إرسال انمنع خلال ٧ أيام. لم يُعد إرسال أي مقطع."
    by_sha = {e.get("video_sha"): e for e in rendered(events)}
    lines = [f"انمنع {ar(len(blocked))} إرسال خلال ٧ أيام (لم يُعد إرسال أي مقطع):"]
    for event in blocked[-6:]:
        shown = by_sha.get(event.get("video_sha")) or {}
        reason = ""
        failure = root / "previews" / str(shown.get("preview_id", "")) / "server-failure.json"
        if shown and failure.is_file():
            reason = str(json.loads(failure.read_text(encoding="utf-8")).get("reason", ""))
        when = time.strftime("%m-%d %H:%M", time.localtime(float(event.get("time", 0))))
        kind = label(kind_of(content(root, str(shown["preview_id"])))) if shown else "؟"
        lines.append(f"• {when} {kind}" + (f" — {reason}" if reason else ""))
    return "\n".join(lines)


def execute(key: str, root: Path, events: list[dict[str, Any]], pending: Any = None) -> str:
    if key not in NAMES:
        raise ValueError("UNKNOWN_TOOL")
    ledger = root / "agent-status.sqlite"
    now = time.time()
    with contextlib.suppress(OSError, ValueError):
        role_status.record(ledger, "snap-bot-v2", key, "RUNNING")
    try:
        if key == "manager":
            text = manager(root, events, pending)
        elif key == "analyst":
            text = analyst(root, events, now)
        elif key == "content_repair":
            text = content_repair(root, events, now)
        else:
            event, value = latest(root, events)
            text = (
                writer(value)
                if key == "writer"
                else reviewer(root, event, value)
                if key == "reviewer"
                else fixer(value)
            )
        state = "SUCCEEDED"
    except Exception:
        text = "تعذر إكمال الأداة؛ ما فيه مقطع صالح أقرأه بعد."
        state = "FAILED"
    with contextlib.suppress(OSError, ValueError):
        role_status.record(ledger, "snap-bot-v2", key, state)
    return "🤖 " + NAMES[key] + "\n" + text
