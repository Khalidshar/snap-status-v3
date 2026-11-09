"""Panel-added news sources open only after Khalid's approval (owner decision 9 Nov 2026)."""

import json
import sqlite3

from studio_core.jobs import panel_workspace
from studio_core.sources import panel

A = "a" * 32
B = "b" * 32


def make_panel(tmp_path):
    root = tmp_path / "state"
    db = root / "snap" / "panel-commands.sqlite"
    db.parent.mkdir(parents=True)
    panel_workspace.submit(
        db,
        A,
        "source_add",
        {"name": "صحيفة", "url": "https://news.example.sa/rss", "command": "saudi"},
    )
    return root, db


def test_candidate_source_is_not_reachable(tmp_path):
    root, db = make_panel(tmp_path)
    assert not (db.parent / panel.FILE).exists()
    assert panel.hosts(root) == set()
    assert panel.approved("snap-bot-v2", "saudi", root) == ()


def test_approval_opens_the_host_for_its_command_only(tmp_path):
    root, db = make_panel(tmp_path)
    with sqlite3.connect(db) as conn:
        source = conn.execute("SELECT id FROM panel_sources").fetchone()[0]
    panel_workspace.submit(db, B, "source_approve", {"source": source})
    data = json.loads((db.parent / panel.FILE).read_text(encoding="utf-8"))
    assert [s["url"] for s in data["sources"]] == ["https://news.example.sa/rss"]
    assert panel.hosts(root) == {"news.example.sa"}
    found = panel.approved("snap-bot-v2", "saudi", root)
    assert [s.url for s in found] == ["https://news.example.sa/rss"] and found[0].kind == "rss"
    assert panel.approved("snap-bot-v2", "world", root) == ()


def test_tampered_export_is_ignored(tmp_path):
    root = tmp_path / "state"
    (root / "status").mkdir(parents=True)
    (root / "status" / panel.FILE).write_text(
        json.dumps(
            {
                "schema": 1,
                "sources": [
                    {"id": "x", "name": "x", "url": "http://plain.example/rss", "command": "world"}
                ],
            }
        ),
        encoding="utf-8",
    )
    assert panel.hosts(root) == set()
