"""Owner-requested written news, fetched through the app's existing Network Core.

The original headline, source, date and link are copied, never paraphrased.
This adapter does not approve media, publish a Status, or modify sacred content.
"""

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from studio_core.config import Config
from studio_core.network import NetworkClient
from studio_core.sources.news import NewsUnavailable, collect, validate


class LiveNews:
    def __init__(self, app: str, config_path: Path, history: Path) -> None:
        self.config = Config.load(config_path)
        if app not in {"snap-bot", "status-bot"} or self.config.app != app + "-v2":
            raise ValueError("News application mismatch")
        if any(p.is_symlink() for p in (history.absolute(), *history.absolute().parents)):
            raise ValueError("Unsafe news history")
        history.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.history = history
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS scope(app TEXT PRIMARY KEY)")
            rows = [r[0] for r in db.execute("SELECT app FROM scope")]
            if rows and rows != [app]:
                raise ValueError("Foreign news history")
            db.execute("INSERT OR IGNORE INTO scope VALUES(?)", (app,))
            db.execute(
                "CREATE TABLE IF NOT EXISTS replies(event TEXT PRIMARY KEY, fingerprint TEXT, "
                "state TEXT NOT NULL, at REAL NOT NULL)"
            )
        self.app = app

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.history, timeout=10, isolation_level="IMMEDIATE")
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def reply(
        self,
        command: str,
        event: str,
        send: Callable[[str], Any],
        *,
        client: Any = None,
    ) -> bool:
        """Reserve before work. A crash/unknown send cannot replay this event or title."""
        if not isinstance(event, str) or not 1 <= len(event) <= 128:
            raise ValueError("Invalid news event")
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM replies WHERE event=?", (event,)).fetchone():
                return False
            if (
                db.execute("SELECT count(*) FROM replies WHERE at>?", (now - 60,)).fetchone()[0]
                >= 4
            ):
                raise ValueError("News request rate limit")
            seen = frozenset(
                r[0]
                for r in db.execute(
                    "SELECT fingerprint FROM replies WHERE fingerprint IS NOT NULL AND at>?",
                    (now - 30 * 86400,),
                )
            )
            db.execute("INSERT INTO replies VALUES(?,NULL,'WORKING',?)", (event, now))
        mark: str | None = None
        try:
            try:
                result = collect(command, client or NetworkClient(self.config), now, seen=seen)
                content = result["content"]
                validate(content, time.time())
            except NewsUnavailable:
                content = None
            mark = content["fingerprint"] if content else None
            text = (
                "\n".join(
                    (
                        content["title"],
                        content["headline"],
                        "المصدر: " + content["source"],
                        content["published_label"],
                        content["source_url"],
                    )
                )
                if content
                else "لا يوجد خبر عربي حديث مناسب الآن؛ لم يُستبدل بمحتوى قديم."
            )
            with self.connect() as db:
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
                    "UPDATE replies SET fingerprint=?,state='SEND_INTENT' WHERE event=?",
                    (mark, event),
                )
            send(text)
            with self.connect() as db:
                db.execute("UPDATE replies SET state='ACK' WHERE event=?", (event,))
            return True
        except Exception:
            with self.connect() as db:
                db.execute(
                    "UPDATE replies SET state=? WHERE event=?",
                    ("UNKNOWN" if mark else "FETCH_FAILED", event),
                )
            raise
