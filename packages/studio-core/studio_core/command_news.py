"""Video reply adapter over the existing per-app live-news history and collectors."""

import time
from collections.abc import Callable
from typing import Any

from studio_core.network import NetworkClient
from studio_core.sources.live import LiveNews
from studio_core.sources.news import NewsUnavailable, collect, validate


def reply(
    service: LiveNews,
    command: str,
    event: str,
    send_text: Callable[[str], Any],
    send_clip: Callable[[dict[str, Any]], Any],
    *,
    client: Any = None,
) -> bool:
    """Reserve the existing event/title before rendering; unknown sends never replay."""
    if not isinstance(event, str) or not 1 <= len(event) <= 128:
        raise ValueError("Invalid news event")
    now = time.time()
    with service.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM replies WHERE event=?", (event,)).fetchone():
            return False
        if db.execute("SELECT count(*) FROM replies WHERE at>?", (now - 60,)).fetchone()[0] >= 4:
            raise ValueError("News request rate limit")
        seen = frozenset(
            r[0]
            for r in db.execute(
                "SELECT fingerprint FROM replies WHERE fingerprint IS NOT NULL AND at>?",
                (now - 30 * 86400,),
            )
        )
        db.execute("INSERT INTO replies VALUES(?,NULL,'WORKING',?)", (event, now))
    mark = None
    try:
        try:
            content = collect(command, client or NetworkClient(service.config), now, seen=seen)[
                "content"
            ]
            validate(content, time.time())
        except NewsUnavailable:
            content = None
        mark = content["fingerprint"] if content else None
        with service.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                mark
                and db.execute(
                    "SELECT 1 FROM replies WHERE fingerprint=? AND event<>? AND at>?",
                    (mark, event, now - 30 * 86400),
                ).fetchone()
            ):
                raise ValueError("News headline already reserved")
            db.execute(
                "UPDATE replies SET fingerprint=?,state='SEND_INTENT' WHERE event=?", (mark, event)
            )
        if content:
            send_clip(content)
        else:
            send_text("لا يوجد خبر عربي حديث مناسب الآن؛ لم يُستبدل بمحتوى قديم.")
        with service.connect() as db:
            db.execute("UPDATE replies SET state='ACK' WHERE event=?", (event,))
        return True
    except Exception:
        with service.connect() as db:
            db.execute(
                "UPDATE replies SET state=? WHERE event=?",
                ("UNKNOWN" if mark else "FETCH_FAILED", event),
            )
        raise
