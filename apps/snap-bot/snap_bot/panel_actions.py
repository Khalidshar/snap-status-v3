"""Snap panel executor (owner decision 7 Nov 2026): every queued panel action runs or closes.

produce/retry render one clip into the panel for review; nothing is sent. approve sends that
exact SHA through the owner route once; reject cancels it. Ratings, sources and schedule
preferences are saved by the panel itself; the schedule never starts a timer here.
"""

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from studio_core.jobs import panel_workspace as workspace

UNSUPPORTED = {"upload", "statistics"}  # need their own design/analysis pipelines first


def render_kind(command: str) -> str:
    return "did_you_know" if command in {"facts", "did_you_know"} else command


def preview_command(path: Path, preview: str) -> str | None:
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT command FROM panel_previews WHERE id=?", (preview,)).fetchone()
    return row[0] if row else None


def produce(studio: Any, path: Path, command: str) -> bool:
    from .owner_commands import HALT, KIND_RENDER_UNIT, RENDER_UNIT

    if HALT.exists():
        return False
    kind = render_kind(command)
    unit = RENDER_UNIT if kind == "did_you_know" else KIND_RENDER_UNIT.format(kind=kind)
    if studio.start(unit) != 0:
        return False
    clip = studio.pending()
    if not clip or clip["kind"] != kind or not re.fullmatch("[0-9a-f]{32}", clip["preview"]):
        return False
    video = studio.runtime.root / "previews" / clip["preview"] / "did-you-know.mp4"
    raw = video.read_bytes()
    workspace.record_preview(path, clip["preview"], command, raw, [str(clip["title"])[:60]])
    return True


def rate(path: Path, preview: str, rating: int) -> bool:
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS panel_ratings(preview TEXT PRIMARY KEY, rating INTEGER,"
            " at REAL)"
        )
        db.execute(
            "INSERT INTO panel_ratings VALUES(?,?,?) ON CONFLICT(preview) DO UPDATE SET"
            " rating=excluded.rating, at=excluded.at",
            (preview, rating, time.time()),
        )
    return True


def run(studio: Any, path: Path, item: dict[str, Any]) -> bool:
    from .owner_commands import SENT

    action, payload = item["action"], item["payload"]
    if action == "produce":
        return produce(studio, path, payload["command"])
    if action == "retry":
        command = preview_command(path, payload["preview"])
        return bool(command) and produce(studio, path, str(command))
    if action == "approve":
        return str(studio.deliver(payload["sha"])).startswith(SENT)
    if action == "reject":
        state = studio.state()
        state["cancelled"] = (state["cancelled"] + [payload["sha"]])[-200:]
        studio.save(state)
        return True
    if action == "rate":
        return rate(path, payload["preview"], int(payload["rating"]))
    if action in {"source_add", "source_approve", "schedule"}:
        return True  # saved by the panel at submit; no timer is ever started
    if action == "server":
        if payload["target"] != "wsl" or payload["operation"] not in {"stop", "start"}:
            return False  # Amazon is stopped; old/new switching is not on this server
        if payload["operation"] == "stop":
            studio.halt()
        else:
            studio.unhalt()
        return True
    return False  # UNSUPPORTED actions close as UNKNOWN, never silently retried


def process(studio: Any, path: Path) -> bool:
    """One queued panel action. True when an action was taken from the queue."""
    if not path.is_file():
        return False
    item = workspace.claim(path)
    if item is None:
        return False
    ok = False
    try:
        ok = run(studio, path, item)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, json.JSONDecodeError):
        ok = False
    finally:
        workspace.finish(path, item["id"], ok)
    return True
