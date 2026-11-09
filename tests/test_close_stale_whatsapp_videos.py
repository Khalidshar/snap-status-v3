import importlib.util
import sqlite3
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ops/close_stale_whatsapp_videos.py"
SPEC = importlib.util.spec_from_file_location("close_stale_whatsapp_videos", SCRIPT)
assert SPEC and SPEC.loader
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "commands #1.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE command_videos(tag TEXT PRIMARY KEY, kind TEXT, state TEXT, at REAL,"
            " acked_at REAL, message_tag TEXT, code TEXT)"
        )
        old = tool.FIX_MERGED_AT - 1
        db.executemany(
            "INSERT INTO command_videos VALUES (?, 'riddle', ?, ?, NULL, ?, 'existing')",
            [
                ("stale", "WORKING", old, None),
                ("boundary", "WORKING", tool.FIX_MERGED_AT, None),
                ("has-proof", "WORKING", old, "message"),
                ("pending-send", "SEND_INTENT", old, None),
                ("accepted", "API_ACCEPTED", old, "message"),
            ],
        )
    return path


def test_dry_run_and_apply_preserve_every_other_row(database):
    original = database.read_bytes()
    rows = tool.inspect(database)
    assert [(row["tag"], row["state"]) for row in rows] == [("stale", "WORKING")]
    assert database.read_bytes() == original
    assert tool.inspect(database, apply=True) == [dict(rows[0], state="BLOCKED_OR_UNKNOWN")]
    assert tool.inspect(database, apply=True) == []
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT tag,state,code FROM command_videos ORDER BY tag").fetchall() == [
            ("accepted", "API_ACCEPTED", "existing"),
            ("boundary", "WORKING", "existing"),
            ("has-proof", "WORKING", "existing"),
            ("pending-send", "SEND_INTENT", "existing"),
            ("stale", "BLOCKED_OR_UNKNOWN", tool.REASON),
        ]


def test_apply_refuses_an_active_worker(database):
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE component_locks(app TEXT, component TEXT, deadline REAL)")
        db.execute(
            "INSERT INTO component_locks VALUES ('status-bot-v2','command-clips',?)",
            (time.time() + 60,),
        )
    with pytest.raises(ValueError, match="WA_COMMAND_CLIPS_ACTIVE"):
        tool.inspect(database, apply=True)
    assert len(tool.inspect(database)) == 1


def test_missing_database_is_never_created(tmp_path):
    path = tmp_path / "missing.sqlite"
    for apply in (False, True):
        with pytest.raises(FileNotFoundError):
            tool.inspect(path, apply=apply)
    assert not path.exists()
