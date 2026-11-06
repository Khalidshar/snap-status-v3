import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from studio_core.commands import BY_KEY
from studio_core.jobs import panel_queue


@pytest.mark.parametrize(
    ("inbound", "expected"),
    [(None, "UNKNOWN"), (0, "UNKNOWN"), (99_999, "DONE"), (100_001, "UNKNOWN")],
)
def test_transfer_preserves_whatsapp_window_and_never_replays(tmp_path, inbound, expected):
    path = tmp_path / "panel.sqlite"
    request_id = "a" * 32
    with sqlite3.connect(":memory:") as db:
        db.execute(
            "CREATE TABLE whatsapp_windows(identity_binding TEXT PRIMARY KEY, last_inbound_at)"
        )
        db.execute(
            "CREATE TABLE whatsapp_commands(tag TEXT PRIMARY KEY, binding, body, at, state, code)"
        )
        if inbound is not None:
            db.execute("INSERT INTO whatsapp_windows VALUES ('owner', ?)", (inbound,))
        db.commit()

        @contextmanager
        def transaction():
            with db:
                yield db

        outbox = SimpleNamespace(
            repo=SimpleNamespace(transaction=transaction, clock=lambda: 100_000),
            identity=SimpleNamespace(binding="owner", tag=lambda kind, value: f"{kind}:{value}"),
        )
        assert not panel_queue.transfer_status(path, outbox)
        assert panel_queue.enqueue(path, "news", request_id)
        assert panel_queue.transfer_status(path, outbox)
        assert not panel_queue.transfer_status(path, outbox)
        assert db.execute("SELECT last_inbound_at FROM whatsapp_windows").fetchall() == (
            [] if inbound is None else [(inbound,)]
        )
        assert db.execute("SELECT tag, binding, body, state FROM whatsapp_commands").fetchall() == (
            [(f"panel:{request_id}", "owner", BY_KEY["news"].label, "QUEUED")]
            if expected == "DONE"
            else []
        )
    db.close()
    with panel_queue.connect(path) as local:
        assert local.execute("SELECT state FROM panel_commands").fetchone()[0] == expected


def test_inbox_rejects_conflicting_ids_rate_and_symlinks(tmp_path):
    path = tmp_path / "panel.sqlite"
    request_id = "0" * 32
    with pytest.raises(ValueError, match="INVALID_PANEL_COMMAND"):
        panel_queue.enqueue(path, "invalid", request_id)
    assert panel_queue.enqueue(path, "news", request_id)
    with pytest.raises(ValueError, match="COMMAND_ID_CONFLICT"):
        panel_queue.enqueue(path, "verse", request_id)
    for index in range(1, 10):
        assert panel_queue.enqueue(path, "news", f"{index:032x}")
    with pytest.raises(ValueError, match="PANEL_RATE_LIMIT"):
        panel_queue.enqueue(path, "news", "f" * 32)
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="UNSAFE_PANEL_QUEUE"):
        panel_queue.claim(alias)
