"""Acceptance uses fake state and never contacts systemd in tests."""

import hashlib
import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_v3_acceptance.py"
SPEC = importlib.util.spec_from_file_location("check_v3_acceptance", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)

PRIVATE_MARKER = "PRIVATE_CONTENT_DO_NOT_PRINT"


def fixture(tmp_path):
    snap = tmp_path / "snap"
    status = tmp_path / "status"
    for app in (snap, status):
        commands = app / "commands"
        commands.mkdir(parents=True)
        output = app / "production"
        output.mkdir()
        (output / "video.mp4").write_bytes(b"fixture")
        (output / "report.json").write_text(
            json.dumps(
                {
                    "app": f"{app.name}-bot-v2",
                    "qa": {"result": "PASS", "duration": 41.2, "private": PRIVATE_MARKER},
                    "design": {
                        "agent": "designer",
                        "style": "mix",
                        "inspection": {"bars": []},
                        "reasons": [PRIVATE_MARKER],
                    },
                }
            )
        )
    (snap / "commands/listener-health.json").write_text(
        json.dumps({"heartbeat": 1793833200, "offset": PRIVATE_MARKER})
    )
    with closing(sqlite3.connect(snap / "commands/replies.sqlite")) as db, db:
        db.execute("CREATE TABLE replies(key TEXT, state TEXT)")
        db.execute("INSERT INTO replies VALUES (?, 'ACK')", (PRIVATE_MARKER,))
    with closing(sqlite3.connect(status / "commands/jobs.sqlite")) as db, db:
        db.execute("CREATE TABLE whatsapp_commands(tag TEXT, state TEXT, at REAL)")
        db.execute("INSERT INTO whatsapp_commands VALUES (?, 'ACK', 1793833200)", (PRIVATE_MARKER,))
        db.execute("CREATE TABLE whatsapp_outbox(job_id TEXT, state TEXT)")
    return snap, status


def snapshot(root):
    return {item.relative_to(root): item.read_bytes() for item in root.rglob("*") if item.is_file()}


def test_success_uses_only_safe_summary_and_never_mutates(tmp_path, monkeypatch, capsys):
    fixture(tmp_path)
    monkeypatch.setattr(check, "active", lambda _: True)
    before = snapshot(tmp_path)
    assert check.main(["--state-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "نبض المستمع" in output and "آخر أمر وصل عبر الويبهوك" in output
    assert output.count("الجودة ناجحة · التصميم ناجح (مختلط) · ٤١.٢ ثانية") == 2
    assert PRIVATE_MARKER not in output and str(tmp_path) not in output
    assert snapshot(tmp_path) == before


def test_unknown_and_duplicates_fail(tmp_path, monkeypatch, capsys):
    snap, status = fixture(tmp_path)
    monkeypatch.setattr(check, "active", lambda _: True)
    intent = {
        "kind": "INTENT",
        "video_sha": PRIVATE_MARKER,
        "seq": 1,
        "previous": "0" * 64,
    }
    canonical = json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
    intent["hash"] = hashlib.sha256(canonical).hexdigest()
    (snap / "events.jsonl").write_text(json.dumps(intent) + "\n")
    with closing(sqlite3.connect(status / "commands/jobs.sqlite")) as db, db:
        db.executemany(
            "INSERT INTO whatsapp_outbox VALUES (?, ?)",
            [(PRIVATE_MARKER, "DELIVERY_UNKNOWN"), (PRIVATE_MARKER, "SENT")],
        )
    assert check.main(["--state-root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "إرسال غير محسوم 1، طلبات مكررة 0" in output
    assert "إرسال غير محسوم 1، طلبات مكررة 1" in output
    assert PRIVATE_MARKER not in output


def test_missing_evidence_does_not_create_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check, "active", lambda _: False)
    assert check.main(["--state-root", str(tmp_path)]) == 1
    assert "تعذر التحقق" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_observes_live_wal_and_closed_wal_without_creating_sidecars(tmp_path):
    path = tmp_path / "jobs.sqlite"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE sample(value TEXT)")
        writer.execute("INSERT INTO sample VALUES (?)", (PRIVATE_MARKER,))
        writer.commit()
        with closing(check.database(path)) as reader:
            assert reader.execute("SELECT value FROM sample").fetchone() == (PRIVATE_MARKER,)
    before = snapshot(tmp_path)
    with closing(check.database(path)) as reader:
        assert reader.execute("SELECT value FROM sample").fetchone() == (PRIVATE_MARKER,)
    assert snapshot(tmp_path) == before
