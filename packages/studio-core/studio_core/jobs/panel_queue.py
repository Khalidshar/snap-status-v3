"""Durable local owner UI inbox. Reservation precedes side effects; no replay."""

import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from studio_core.commands import BY_KEY


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    if any(p.is_symlink() for p in (path, *path.absolute().parents)):
        raise ValueError("UNSAFE_PANEL_QUEUE")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous=FULL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS panel_commands(id TEXT PRIMARY KEY, "
        "command TEXT NOT NULL, at REAL NOT NULL, state TEXT NOT NULL)"
    )
    try:
        with db:
            yield db
    finally:
        db.close()


def enqueue(path: Path, key: str, request_id: str) -> bool:
    if key not in BY_KEY or not re.fullmatch("[0-9a-f]{32}", request_id):
        raise ValueError("INVALID_PANEL_COMMAND")
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT command FROM panel_commands WHERE id=?", (request_id,)).fetchone()
        if old:
            if old[0] != key:
                raise ValueError("COMMAND_ID_CONFLICT")
            return False
        if (
            db.execute(
                "SELECT count(*) FROM panel_commands WHERE at>?", (time.time() - 60,)
            ).fetchone()[0]
            >= 10
        ):
            raise ValueError("PANEL_RATE_LIMIT")
        db.execute(
            "INSERT INTO panel_commands VALUES(?,?,?,?)", (request_id, key, time.time(), "QUEUED")
        )
    path.chmod(0o600)
    return True


def claim(path: Path) -> dict[str, Any] | None:
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE panel_commands SET state='EXPIRED' WHERE state='QUEUED' AND at<?",
            (time.time() - 900,),
        )
        row = db.execute(
            "SELECT * FROM panel_commands WHERE state='QUEUED' ORDER BY at,id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        db.execute("UPDATE panel_commands SET state='INTENT' WHERE id=?", (row["id"],))
        return dict(row)


def finish(path: Path, request_id: str, ok: bool) -> None:
    with connect(path) as db:
        db.execute(
            "UPDATE panel_commands SET state=? WHERE id=? AND state='INTENT'",
            ("DONE" if ok else "UNKNOWN", request_id),
        )


def transfer_status(path: Path, outbox: Any) -> bool:
    """Use the existing identity-bound WhatsApp queue; never renew its window."""
    if not path.is_file():
        return False
    item = claim(path)
    if item is None:
        return False
    from studio_core.commands import BY_KEY

    try:
        with outbox.repo.transaction() as db:
            row = db.execute(
                "SELECT last_inbound_at FROM whatsapp_windows WHERE identity_binding=?",
                (outbox.identity.binding,),
            ).fetchone()
            now = outbox.repo.clock()
            if row is None or not 0 <= now - row[0] < 86400:
                raise ValueError("WA_SERVICE_WINDOW_CLOSED")
            tag = outbox.identity.tag("panel", item["id"])
            db.execute(
                "INSERT OR IGNORE INTO whatsapp_commands VALUES(?,?,?,?,?,NULL)",
                (tag, outbox.identity.binding, BY_KEY[item["command"]].label, now, "QUEUED"),
            )
        finish(path, item["id"], True)
    except Exception:
        finish(path, item["id"], False)
    return True
