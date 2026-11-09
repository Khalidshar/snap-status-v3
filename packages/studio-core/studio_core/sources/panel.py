"""News sources Khalid adds and approves in the panel (owner decision 9 Nov 2026).

A source added in the panel stays a candidate: nothing connects to it. Khalid's approval
exports it here; only then may the source broker reach its host, and only the news command
it was added for reads it. HTTPS RSS/Atom only, validated like the approved catalog.
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import Source, _text, _url

STATE = Path("/var/lib/madar")
FILE = "panel-sources.json"
APPS = ("snap", "status")


def short(app: str) -> str:
    return "snap" if app.startswith("snap") else "status"


def export(database: Path) -> Path:
    """Write the approved panel sources next to the panel database, atomically."""
    with sqlite3.connect(database) as db:
        rows = db.execute(
            "SELECT id,name,url,command FROM panel_sources WHERE state='approved' ORDER BY id"
        ).fetchall()
    items = []
    for identifier, name, url, command in rows:
        try:
            items.append(
                {"id": identifier, "name": _text(name)[:80], "url": _url(url), "command": command}
            )
        except ValueError:
            continue  # an unsafe row is never exported
    target = database.parent / FILE
    fd, temp = tempfile.mkstemp(dir=database.parent, prefix=".panel-sources-")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"schema": 1, "sources": items}, stream, ensure_ascii=False)
    os.chmod(temp, 0o644)
    os.replace(temp, target)
    return target


def _read(app: str, root: Path = STATE) -> list[dict[str, Any]]:
    path = root / short(app) / FILE
    if path.is_symlink() or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for item in data.get("sources", []) if isinstance(data, dict) else []:
        try:
            out.append(
                {
                    "id": str(item["id"]),
                    "name": _text(item["name"]),
                    "url": _url(item["url"]),
                    "command": str(item["command"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def approved(app: str, command: str, root: Path = STATE) -> tuple[Source, ...]:
    return tuple(
        Source(
            id="panel-" + item["id"],
            name=item["name"],
            url=item["url"],
            commands=(command,),
            kind="rss",
            language="ar",
            status="approved",
            permission="unverified",
            note="أضافه خالد من اللوحة",
            evidence="panel approval",
        )
        for item in _read(app, root)
        if item["command"] == command
    )


def hosts(root: Path = STATE) -> set[str]:
    """Hosts of every panel-approved source, for the source broker's allowlist."""
    found = set()
    for app in APPS:
        for item in _read(app, root):
            host = urlsplit(item["url"]).hostname
            if host:
                found.add(host)
    return found
