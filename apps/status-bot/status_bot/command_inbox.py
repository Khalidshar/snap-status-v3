"""Durable owner-command inbox fed only by the authenticated WhatsApp webhook."""

import sqlite3
from typing import Any

from .whatsapp_contract import identifier


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS whatsapp_commands("
        "tag TEXT PRIMARY KEY, identity_binding TEXT NOT NULL, text TEXT NOT NULL, "
        "at REAL NOT NULL, state TEXT NOT NULL, code TEXT)"
    )


def text_of(item: dict[str, Any]) -> str | None:
    value: Any = None
    if item.get("type") == "text" and isinstance(item.get("text"), dict):
        value = item["text"].get("body")
    elif item.get("type") == "interactive" and isinstance(item.get("interactive"), dict):
        data = item["interactive"]
        kind = data.get("type")
        if kind in {"list_reply", "button_reply"} and isinstance(data.get(kind), dict):
            value = data[kind].get("id")
            if isinstance(value, str) and value.startswith("c:") and value[2:].isdecimal():
                value = value[2:]
    if not isinstance(value, str) or not 1 <= len(value) <= 600:
        return None
    return value


def enqueue(db: sqlite3.Connection, identity: Any, item: dict[str, Any], now: float) -> None:
    """Called in the same commit as verified receipts, never before signature validation."""
    if (
        item.get("from") != identity.recipient
        or not identifier(item.get("id"), r"wamid\.[A-Za-z0-9+/=_-]{1,250}")
        or not 0 <= now - int(item["timestamp"]) <= 900
    ):
        return
    text = text_of(item)
    if text is None:
        return
    tag = identity.tag("command", item["id"])
    if db.execute("SELECT 1 FROM whatsapp_commands WHERE tag=?", (tag,)).fetchone():
        return
    rate = db.execute(
        "SELECT count(*) FROM whatsapp_commands WHERE identity_binding=? AND at>?",
        (identity.binding, now - 60),
    ).fetchone()[0]
    queued = db.execute(
        "SELECT count(*) FROM whatsapp_commands WHERE state='QUEUED' AND identity_binding=?",
        (identity.binding,),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO whatsapp_commands VALUES(?,?,?,?,?,NULL)",
        (
            tag,
            identity.binding,
            text,
            now,
            "QUEUED" if rate < 10 and queued < 20 else "RATE_LIMITED",
        ),
    )


def claim(repo: Any, identity: Any) -> dict[str, Any] | None:
    with repo.transaction() as db:
        initialize(db)
        now = repo.clock()
        db.execute(
            "UPDATE whatsapp_commands SET state='EXPIRED' WHERE state='QUEUED' AND at<?",
            (now - 900,),
        )
        row = db.execute(
            "SELECT * FROM whatsapp_commands WHERE identity_binding=? AND state='QUEUED' "
            "ORDER BY at,tag LIMIT 1",
            (identity.binding,),
        ).fetchone()
        if row is None:
            return None
        db.execute("UPDATE whatsapp_commands SET state='WORKING' WHERE tag=?", (row["tag"],))
        return dict(row)
