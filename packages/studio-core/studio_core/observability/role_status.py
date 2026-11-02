"""Eighteen fixed identities in an app-local append-only execution ledger."""

import sqlite3
import time
from pathlib import Path
from typing import Any

from studio_core.observability.agent_inventory import APPLICATIONS, ROLE_IDS

STATES = {"RUNNING", "SUCCEEDED", "FAILED", "UNKNOWN"}


def record(path: Path, app: str, role: str, state: str) -> None:
    if app not in APPLICATIONS or role not in ROLE_IDS or state not in STATES:
        raise ValueError("INVALID_AGENT_IDENTITY")
    if any(p.is_symlink() for p in (path, *path.absolute().parents)):
        raise ValueError("UNSAFE_AGENT_LEDGER")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path, timeout=5)
    try:
        with db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS scope(app TEXT PRIMARY KEY)")
            known = [r[0] for r in db.execute("SELECT app FROM scope")]
            if known and known != [app]:
                raise ValueError("FOREIGN_AGENT_LEDGER")
            db.execute("INSERT OR IGNORE INTO scope VALUES(?)", (app,))
            db.execute("CREATE TABLE IF NOT EXISTS identities(role TEXT PRIMARY KEY)")
            db.executemany("INSERT OR IGNORE INTO identities VALUES(?)", [(r,) for r in ROLE_IDS])
            if {r[0] for r in db.execute("SELECT role FROM identities")} != set(ROLE_IDS):
                raise ValueError("AGENT_REGISTRY_CHANGED")
            db.execute(
                "CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,"
                "role TEXT,state TEXT,at REAL)"
            )
            db.execute("INSERT INTO runs(role,state,at) VALUES(?,?,?)", (role, state, time.time()))
    finally:
        db.close()
    path.chmod(0o600)


def snapshot(path: Path, app: str) -> list[dict[str, Any]]:
    if app not in APPLICATIONS:
        raise ValueError("INVALID_APP")
    latest: dict[str, tuple[str, float]] = {}
    if path.is_file() and not any(p.is_symlink() for p in (path, *path.absolute().parents)):
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            if [r[0] for r in db.execute("SELECT app FROM scope")] != [app]:
                raise ValueError("FOREIGN_AGENT_LEDGER")
            for role, state, at in db.execute("SELECT role,state,at FROM runs ORDER BY id"):
                if role in ROLE_IDS and state in STATES:
                    latest[role] = (state, at)
        finally:
            db.close()
    return [
        {
            "id": role,
            "state": latest.get(role, ("UNKNOWN", None))[0],
            "at": latest.get(role, ("UNKNOWN", None))[1],
        }
        for role in ROLE_IDS
    ]
