"""War-only automatic alert computation; app-owned guarded transports stay separate."""

import time
import uuid
from collections.abc import Callable
from typing import Any

from studio_core.network import NetworkClient
from studio_core.sources.news import NewsUnavailable, collect, validate

WAR_WORDS = (
    "حرب",
    "قصف",
    "غارة",
    "غارات",
    "صاروخ",
    "صواريخ",
    "اشتباك",
    "اجتياح",
    "هجوم عسكري",
    "هجوم مسلح",
    "مسيّرة",
    "عدوان",
    "وقف إطلاق النار",
)


def is_war(title: str) -> bool:
    return any(word in title for word in WAR_WORDS)


def tick(
    service: Any,
    approved: frozenset[str],
    allowed: Callable[[], bool],
    send: Callable[[str], Any],
    *,
    enabled: bool = False,
    client: Any = None,
) -> bool:
    if not enabled or not approved or not allowed():
        return False
    now = time.time()
    with service.connect() as db:
        seen = frozenset(
            row[0]
            for row in db.execute(
                "SELECT fingerprint FROM replies WHERE fingerprint IS NOT NULL AND at>?",
                (now - 30 * 86400,),
            )
        )
    try:
        content = collect(
            "urgent",
            client or NetworkClient(service.config),
            now,
            seen=seen,
            source_ids=approved,
            predicate=is_war,
        )["content"]
    except NewsUnavailable:
        return False
    validate(content, time.time())
    if content["source_id"] not in approved or not is_war(content["headline"]) or not allowed():
        return False
    mark, event = content["fingerprint"], "war-" + uuid.uuid4().hex
    with service.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute(
            "SELECT 1 FROM replies WHERE fingerprint=? AND at>?", (mark, now - 30 * 86400)
        ).fetchone():
            return False
        db.execute("INSERT INTO replies VALUES(?,?,'SEND_INTENT',?)", (event, mark, now))
    try:
        # Re-check full stop/window immediately before the only send attempt.
        if not allowed():
            raise ValueError("ALERT_BLOCKED")
        validate(content, time.time())
        send(
            "\n".join(
                (
                    content["title"],
                    content["headline"],
                    "المصدر: " + content["source"],
                    content["published_label"],
                    content["source_url"],
                )
            )
        )
        state = "ACK"
    except Exception:
        state = "UNKNOWN"
    with service.connect() as db:
        db.execute("UPDATE replies SET state=? WHERE event=?", (state, event))
    return state == "ACK"
