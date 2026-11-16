"""Give back fashion stories held by clips that were never sent (16 Nov decision)."""

import os
import sqlite3
from pathlib import Path
from typing import Any

from .fashion_policy import KEYS as FASHION_KEYS


def history_path(app: str) -> Path:
    short = "snap" if app == "snap-bot-v2" else "status"
    root = os.getenv("MADAR_PRODUCTION_STATE_ROOT", f"/var/lib/madar/{short}/production")
    return Path(root) / "history.sqlite"


def _delete(db: Path, where: str, values: tuple[Any, ...]) -> int:
    if not db.is_file() or any(p.is_symlink() for p in (db, *db.absolute().parents)):
        return 0
    marks = ",".join("?" * len(FASHION_KEYS))
    with sqlite3.connect(db) as conn:
        return conn.execute(
            f"DELETE FROM used WHERE kind IN ({marks}) AND {where}",  # noqa: S608 - fixed text
            (*sorted(FASHION_KEYS), *values),
        ).rowcount


def release_job(db: Path, job: str) -> int:
    """A failed build: free what this job reserved."""
    return _delete(db, "job=?", (job,))


def story_urls(content: Any) -> list[str]:
    plan = content.get("legacy_production") if isinstance(content, dict) else None
    if not isinstance(plan, dict) or plan.get("key") not in FASHION_KEYS:
        return []
    return sorted(
        {
            s["source_url"]
            for s in plan.get("scenes", [])
            if isinstance(s, dict) and isinstance(s.get("source_url"), str) and s["source_url"]
        }
    )


def release_content(db: Path, content: Any) -> int:
    """A cancelled, rejected or expired unsent clip. A reserved story belongs to one job
    only (used() refuses it to every other job), so its fingerprints identify this clip."""
    urls = story_urls(content)
    if not urls:
        return 0
    return _delete(db, f"fingerprint IN ({','.join('?' * len(urls))})", tuple(urls))
