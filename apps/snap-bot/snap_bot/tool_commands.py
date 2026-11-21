"""«أدوات الوكلاء» in the Snap bot (owner 21 Nov 2026: the six tools answered «غير مربوطة»).

Each tool reads Snap's own evidence — the preview event log, the clips' content and their
agent ledgers — and answers in one message. Read-only: nothing is rendered, sent, approved,
edited or retried, and sacred text is never touched. Shared wording lives in
studio_core.observability.agent_tools so both bots answer alike.
"""

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from studio_core.observability import agent_tools as shared
from studio_core.observability.agent_tools import DAY, NAMES, ar, kind_of, label

__all__ = ["NAMES", "execute"]


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


def manager(root: Path, pending: Any) -> str:
    if pending:
        first = "⏳ فيه مقطع ينتظر موافقتك: " + str(pending.get("title", ""))
    else:
        first = "✅ جاهز لتلقي الأوامر؛ ما فيه مقطع ينتظر."
    return "\n".join([first, *shared.status_lines(root)])


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
    now = time.time()

    def work() -> str:
        if key == "manager":
            return manager(root, pending)
        if key == "analyst":
            return analyst(root, events, now)
        if key == "content_repair":
            return content_repair(root, events, now)
        event, value = latest(root, events)
        if key == "writer":
            return shared.writer(value)
        if key == "reviewer":
            ledger = root / "previews" / str(event["preview_id"]) / "state" / "jobs.sqlite"
            return shared.reviewer(ledger, value)
        return shared.fixer(value)

    return shared.run("snap-bot-v2", key, root / "agent-status.sqlite", work)
