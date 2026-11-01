"""Shared frozen daily-brief content; no app identity, transport or scheduling."""

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from studio_core.network import NetworkClient
from studio_core.presentation import civil_calendar
from studio_core.presentation.did_you_know import load_kind, make_content, validate_content
from studio_core.sources.live import LiveNews
from studio_core.sources.news import DIGITS, collect, digest, published_label, validate


def dates(now: float) -> tuple[str, str]:
    day = datetime.fromtimestamp(now, ZoneInfo("Asia/Riyadh"))
    year, month, hijri_day = civil_calendar.hijri(day.date())
    return (
        published_label(now),
        f"{hijri_day} {civil_calendar.MONTHS[month - 1]} {year} هـ".translate(DIGITS),
    )


def validate_brief(value: dict[str, Any], now: float) -> None:
    if value.get("schema") != 1 or value.get("content_hash") != digest(value):
        raise ValueError("Brief snapshot changed")
    if (
        value.get("dates") != list(dates(value["at"]))
        or not value["at"] <= now < value["expires_at"]
    ):
        raise ValueError("Brief dates or expiry changed")
    validate_content(value["faith"])
    if value["faith"].get("kind") != "verse":
        raise ValueError("Brief faith kind changed")
    for key in ("saudi", "tech"):
        validate(value[key], now)
        if value[key]["command"] != key:
            raise ValueError("Brief news category changed")
    if value["expires_at"] != min(value[k]["expires_at"] for k in ("faith", "saudi", "tech")):
        raise ValueError("Brief expiry changed")


def prepare(service: LiveNews, event: str, *, client: Any = None) -> dict[str, Any] | None:
    if not isinstance(event, str) or not 1 <= len(event) <= 100:
        raise ValueError("Invalid brief event")
    tag, now = "brief:" + event, time.time()
    with service.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM replies WHERE event=?", (tag,)).fetchone():
            return None
        if db.execute("SELECT count(*) FROM replies WHERE at>?", (now - 60,)).fetchone()[0] >= 4:
            raise ValueError("Brief rate limit")
        seen = frozenset(
            r[0]
            for r in db.execute(
                "SELECT fingerprint FROM replies WHERE fingerprint IS NOT NULL AND at>?",
                (now - 30 * 86400,),
            )
        )
        db.execute("INSERT INTO replies VALUES(?,NULL,'WORKING',?)", (tag, now))
    try:
        client = client or NetworkClient(service.config)
        news = {k: collect(k, client, now, seen=seen)["content"] for k in ("saudi", "tech")}
        facts = load_kind("verse")
        day = datetime.fromtimestamp(now, ZoneInfo("Asia/Riyadh")).date()
        faith = make_content(facts[day.toordinal() % len(facts)], now, "verse")
        value = {
            "schema": 1,
            "at": now,
            "dates": list(dates(now)),
            "faith": faith,
            **news,
            "expires_at": min(faith["expires_at"], *(c["expires_at"] for c in news.values())),
        }
        value["content_hash"] = digest(value)
        validate_brief(value, time.time())
        marks = [news[k]["fingerprint"] for k in ("saudi", "tech")]
        if len(set(marks)) != 2:
            raise ValueError("Brief headlines duplicate")
        with service.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key, mark in zip(("saudi", "tech"), marks, strict=True):
                if db.execute(
                    "SELECT 1 FROM replies WHERE fingerprint=? AND at>?", (mark, now - 30 * 86400)
                ).fetchone():
                    raise ValueError("Brief headline already reserved")
                db.execute(
                    "INSERT INTO replies VALUES(?,?,'SEND_INTENT',?)", (tag + ":" + key, mark, now)
                )
            db.execute("UPDATE replies SET state='SEND_INTENT' WHERE event=?", (tag,))
        return value
    except Exception:
        with service.connect() as db:
            db.execute("UPDATE replies SET state='FETCH_FAILED' WHERE event=?", (tag,))
        raise
