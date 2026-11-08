"""Owner workspace in each bot's existing panel queue; append-only audit, no network.

Actions are deliberately separate from chat commands: producing must never invoke a
chat handler which renders and sends. Workers reserve before invoking owned adapters.
"""

import hashlib
import ipaddress
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from studio_core.jobs.panel_queue import connect
from studio_core.sources import CONTENT_KEYS

ACTIONS = {
    "produce",
    "upload",
    "approve",
    "reject",
    "retry",
    "source_add",
    "source_approve",
    "schedule",
    "server",
    "statistics",
    "rate",
}


def database(path: Path) -> Any:
    return connect(path)


def initialize(db: Any) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS panel_actions(
          id TEXT PRIMARY KEY, action TEXT NOT NULL, payload TEXT NOT NULL,
          at REAL NOT NULL, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS panel_audit(
          seq INTEGER PRIMARY KEY, at REAL NOT NULL, action_id TEXT NOT NULL,
          event TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS panel_previews(
          id TEXT PRIMARY KEY, command TEXT NOT NULL, sha TEXT NOT NULL,
          state TEXT NOT NULL, at REAL NOT NULL, sources TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS panel_sources(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT UNIQUE NOT NULL,
          command TEXT NOT NULL, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS panel_schedules(
          command TEXT PRIMARY KEY, clock TEXT NOT NULL, enabled INTEGER NOT NULL);
    """)


def audit(db: Any, identifier: str, event: str) -> None:
    db.execute(
        "INSERT INTO panel_audit(at,action_id,event) VALUES(?,?,?)",
        (time.time(), identifier, event),
    )


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{32}", value) is not None


def validate(action: str, payload: dict[str, Any]) -> None:
    fields = {
        "produce": {"command"},
        "upload": {"asset"},
        "statistics": {"asset"},
        "approve": {"preview", "sha"},
        "reject": {"preview", "sha"},
        "retry": {"preview", "sha"},
        "source_add": {"name", "url", "command"},
        "source_approve": {"source"},
        "schedule": {"command", "clock", "enabled"},
        "server": {"target", "operation", "confirmed"},
        "rate": {"preview", "rating"},
    }
    if action not in ACTIONS or set(payload) != fields[action]:
        raise ValueError("INVALID_PANEL_ACTION")
    if "command" in payload and payload["command"] not in CONTENT_KEYS:
        raise ValueError("INVALID_CONTENT_COMMAND")
    for key in ("asset", "preview", "source"):
        if key in payload and not valid_id(payload[key]):
            raise ValueError("INVALID_IDENTIFIER")
    if "sha" in payload and not re.fullmatch("[0-9a-f]{64}", str(payload["sha"])):
        raise ValueError("INVALID_DIGEST")
    if action == "source_add":
        url = urlsplit(payload["url"])
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.port not in (None, 443)
            or url.query
            or url.fragment
            or url.hostname == "localhost"
            or "." not in url.hostname
            or not isinstance(payload["name"], str)
            or not 1 <= len(payload["name"]) <= 80
            or any(ord(c) < 32 for c in payload["name"] + payload["url"])
        ):
            raise ValueError("INVALID_SOURCE")
        try:
            ipaddress.ip_address(url.hostname)
        except ValueError:
            pass
        else:
            raise ValueError("IP_SOURCE_FORBIDDEN")
    if action == "schedule" and (
        not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(payload["clock"]))
        or type(payload["enabled"]) is not bool
    ):
        raise ValueError("INVALID_SCHEDULE")
    if action == "server" and (
        payload["target"] not in {"amazon", "wsl"}
        or payload["operation"] not in {"stop", "start", "old", "new"}
        or payload["confirmed"] is not True
    ):
        raise ValueError("CONFIRMATION_REQUIRED")
    if action == "rate" and (type(payload["rating"]) is not int or not 1 <= payload["rating"] <= 5):
        raise ValueError("INVALID_RATING")


def submit(path: Path, identifier: str, action: str, payload: dict[str, Any]) -> bool:
    if not valid_id(identifier) or not isinstance(payload, dict):
        raise ValueError("INVALID_PANEL_REQUEST")
    validate(action, payload)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    with database(path) as db:
        initialize(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute(
            "SELECT action,payload FROM panel_actions WHERE id=?", (identifier,)
        ).fetchone()
        if prior:
            if tuple(prior) != (action, encoded):
                raise ValueError("REQUEST_CONFLICT")
            return False
        if (
            db.execute(
                "SELECT count(*) FROM panel_actions WHERE at>?", (time.time() - 60,)
            ).fetchone()[0]
            >= 30
        ):
            raise ValueError("RATE_LIMIT")
        if action in {"approve", "reject", "retry"}:
            preview = db.execute(
                "SELECT sha,state,at FROM panel_previews WHERE id=?", (payload["preview"],)
            ).fetchone()
            if (
                preview is None
                or preview["sha"] != payload["sha"]
                or preview["state"] != "REVIEW"
                or time.time() - preview["at"] > 3600
            ):
                raise ValueError("PREVIEW_NOT_REVIEWABLE")
            state = {"approve": "APPROVED", "reject": "REJECTED", "retry": "SUPERSEDED"}[action]
            db.execute("UPDATE panel_previews SET state=? WHERE id=?", (state, payload["preview"]))
        if action == "source_add":
            db.execute(
                "INSERT INTO panel_sources VALUES(?,?,?,?, 'candidate')",
                (identifier, payload["name"], payload["url"], payload["command"]),
            )
        if action == "source_approve":
            changed = db.execute(
                "UPDATE panel_sources SET state='approved' WHERE id=? AND state='candidate'",
                (payload["source"],),
            ).rowcount
            if changed != 1:
                raise ValueError("SOURCE_NOT_CANDIDATE")
        # Preferences are saved, never enabled as a running timer by the panel.
        if action == "schedule":
            db.execute(
                "INSERT INTO panel_schedules VALUES(?,?,?) ON CONFLICT(command) "
                "DO UPDATE SET clock=excluded.clock,enabled=excluded.enabled",
                (payload["command"], payload["clock"], payload["enabled"]),
            )
        db.execute(
            "INSERT INTO panel_actions VALUES(?,?,?,?, 'QUEUED')",
            (identifier, action, encoded, time.time()),
        )
        audit(db, identifier, "QUEUED")
    return True


def claim(path: Path) -> dict[str, Any] | None:
    with database(path) as db:
        initialize(db)
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE panel_actions SET state='EXPIRED' WHERE state='QUEUED' AND at<?",
            (time.time() - 900,),
        )
        row = db.execute(
            "SELECT * FROM panel_actions WHERE state='QUEUED' ORDER BY at,id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        db.execute("UPDATE panel_actions SET state='INTENT' WHERE id=?", (row["id"],))
        audit(db, row["id"], "INTENT")
        return {**dict(row), "payload": json.loads(row["payload"])}


def finish(path: Path, identifier: str, ok: bool, *, retry_preview: bool = False) -> None:
    with database(path) as db:
        initialize(db)
        db.execute("BEGIN IMMEDIATE")
        state = "DONE" if ok else "UNKNOWN"
        if retry_preview and not ok:
            action = db.execute(
                "SELECT payload FROM panel_actions WHERE id=?"
                " AND action='approve' AND state='INTENT'",
                (identifier,),
            ).fetchone()
            if action is not None:
                payload = json.loads(action["payload"])
                db.execute(
                    "UPDATE panel_previews SET state='REVIEW'"
                    " WHERE id=? AND sha=? AND state='APPROVED'",
                    (payload["preview"], payload["sha"]),
                )
                state = "BLOCKED"
        db.execute(
            "UPDATE panel_actions SET state=? WHERE id=? AND state='INTENT'", (state, identifier)
        )
        audit(db, identifier, state)


def record_preview(
    path: Path, identifier: str, command: str, raw: bytes, sources: list[str]
) -> str:
    if not valid_id(identifier) or command not in CONTENT_KEYS or not raw:
        raise ValueError("INVALID_PREVIEW")
    sha = hashlib.sha256(raw).hexdigest()
    directory = path.parent / "panel-media"
    if any(p.is_symlink() for p in (directory, *directory.absolute().parents)):
        raise ValueError("UNSAFE_MEDIA")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / (identifier + ".mp4")
    if target.is_symlink():
        raise ValueError("UNSAFE_MEDIA")
    with target.open("xb") as stream:
        stream.write(raw)
    target.chmod(0o600)
    with database(path) as db:
        initialize(db)
        db.execute(
            "INSERT INTO panel_previews VALUES(?,?,?,'REVIEW',?,?)",
            (identifier, command, sha, time.time(), json.dumps(sources, ensure_ascii=False)),
        )
        audit(db, identifier, "REVIEW")
    return sha


def view(path: Path) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "previews": [],
        "actions": [],
        "sources": [],
        "schedules": [],
        "audit": [],
    }
    if not path.is_file() or any(p.is_symlink() for p in (path, *path.absolute().parents)):
        return empty
    try:
        with sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            return {
                "previews": [
                    dict(r)
                    for r in db.execute("SELECT * FROM panel_previews ORDER BY at DESC LIMIT 60")
                ],
                "actions": [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,action,at,state FROM panel_actions ORDER BY at DESC LIMIT 60"
                    )
                ],
                "sources": [dict(r) for r in db.execute("SELECT * FROM panel_sources")],
                "schedules": [dict(r) for r in db.execute("SELECT * FROM panel_schedules")],
                "audit": [
                    dict(r)
                    for r in db.execute("SELECT * FROM panel_audit ORDER BY seq DESC LIMIT 100")
                ],
            }
    except sqlite3.Error:
        return empty
