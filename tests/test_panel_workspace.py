import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from studio_core.jobs import panel_workspace as panel


@pytest.mark.parametrize("app", ["snap", "status"])
def test_production_is_reserved_without_delivery(tmp_path, app):
    path = tmp_path / app / "panel-commands.sqlite"
    assert panel.submit(path, "a" * 32, "produce", {"command": "verse"})
    assert not panel.submit(path, "a" * 32, "produce", {"command": "verse"})
    with pytest.raises(ValueError, match="CONFLICT"):
        panel.submit(path, "a" * 32, "produce", {"command": "facts"})
    assert panel.claim(path)["action"] == "produce"
    assert panel.claim(path) is None
    panel.finish(path, "a" * 32, False)
    assert panel.claim(path) is None
    assert panel.view(path)["actions"][0]["state"] == "UNKNOWN"
    assert [r["event"] for r in reversed(panel.view(path)["audit"])] == [
        "QUEUED",
        "INTENT",
        "UNKNOWN",
    ]


@pytest.mark.parametrize("decision", ["approve", "reject", "retry"])
def test_review_requires_exact_unexpired_preview_and_never_replays(tmp_path, decision):
    path = tmp_path / "queue.sqlite"
    sha = panel.record_preview(path, "a" * 32, "facts", b"bound fixture video", ["مصدر معتمد"])
    with pytest.raises(ValueError, match="NOT_REVIEWABLE"):
        panel.submit(path, "b" * 32, decision, {"preview": "a" * 32, "sha": "0" * 64})
    assert panel.submit(path, "c" * 32, decision, {"preview": "a" * 32, "sha": sha})
    assert not panel.submit(path, "c" * 32, decision, {"preview": "a" * 32, "sha": sha})
    with pytest.raises(ValueError, match="NOT_REVIEWABLE"):
        panel.submit(path, "d" * 32, "approve", {"preview": "a" * 32, "sha": sha})
    assert (
        hashlib.sha256((tmp_path / "panel-media" / ("a" * 32 + ".mp4")).read_bytes()).hexdigest()
        == sha
    )


def test_two_reviewers_cannot_make_conflicting_decisions(tmp_path):
    path = tmp_path / "queue.sqlite"
    sha = panel.record_preview(path, "a" * 32, "facts", b"video", [])

    def decide(args):
        identifier, decision = args
        try:
            return panel.submit(path, identifier * 32, decision, {"preview": "a" * 32, "sha": sha})
        except ValueError:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(decide, [("b", "approve"), ("c", "reject")])) == 1


def test_expiry_and_bot_isolation(tmp_path):
    path = tmp_path / "snap" / "queue.sqlite"
    sha = panel.record_preview(path, "a" * 32, "verse", b"video", [])
    with (
        patch.object(panel.time, "time", return_value=panel.time.time() + 3601),
        pytest.raises(ValueError),
    ):
        panel.submit(path, "b" * 32, "approve", {"preview": "a" * 32, "sha": sha})
    with pytest.raises(ValueError):
        panel.submit(
            tmp_path / "status" / "queue.sqlite",
            "b" * 32,
            "approve",
            {"preview": "a" * 32, "sha": sha},
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://news.example/rss",
        "https://127.0.0.1/",
        "https://localhost/",
        "https://user:password@news.example/",
        "https://news.example/?token=secret",
    ],
)
def test_candidate_urls_do_not_create_ssrf_or_credentials(tmp_path, url):
    with pytest.raises(ValueError):
        panel.submit(
            tmp_path / "queue.sqlite",
            "a" * 32,
            "source_add",
            {"name": "مصدر", "url": url, "command": "news"},
        )


def test_candidate_and_schedule_are_durable_without_network_or_timers(tmp_path):
    path = tmp_path / "queue.sqlite"
    panel.submit(
        path,
        "a" * 32,
        "source_add",
        {"name": "مصدر", "url": "https://news.example/rss", "command": "news"},
    )
    assert panel.view(path)["sources"][0]["state"] == "candidate"
    panel.submit(path, "b" * 32, "source_approve", {"source": "a" * 32})
    assert panel.view(path)["sources"][0]["state"] == "approved"
    panel.submit(
        path, "c" * 32, "schedule", {"command": "verse", "clock": "07:30", "enabled": False}
    )
    assert panel.view(path)["schedules"][0]["enabled"] == 0


def test_snapshot_does_not_create_or_modify_database(tmp_path):
    path = tmp_path / "queue.sqlite"
    assert panel.view(path)["actions"] == []
    assert not path.exists()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE existing(id TEXT)")
    before = path.read_bytes()
    assert panel.view(path)["actions"] == []
    assert path.read_bytes() == before


def test_server_requires_explicit_confirmation(tmp_path):
    with pytest.raises(ValueError, match="CONFIRMATION"):
        panel.submit(
            tmp_path / "queue.sqlite",
            "a" * 32,
            "server",
            {"target": "amazon", "operation": "new", "confirmed": False},
        )
