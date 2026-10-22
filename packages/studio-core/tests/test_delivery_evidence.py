"""Shared delivery evidence. Ported from the lab core 0.11.0 guards (test_delivery_core)."""

import json
import os
import shutil
import socket
import sqlite3
import stat
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from studio_core import delivery
from studio_core.config import Config
from studio_core.jobs import Repository
from studio_core.storage import Storage

BOT = "khalid_snap_v2_test_bot"
SHA = "a" * 64


def make_config(root: Path) -> Config:
    return Config(
        "snap-bot-v2",
        root,
        {"scheduler": ("owner",)},
        ("demo",),
        {"CRITICAL": 180, "HIGH": 600, "NORMAL": 600, "BACKGROUND": 1200},
        30,
        60,
        10,
        3,
        60,
        60,
        86400,
        root / "proxy.sock",
        ("allowed.example",),
        2,
        4096,
        1,
    )


class Published:
    """A job whose Telegram ACK is durably PUBLISHED, as an application's deliver() leaves it."""

    def __init__(self, root: Path) -> None:
        import time

        self.config = make_config(root)
        self.repo = Repository(self.config)
        job, _ = self.repo.enqueue("k1", "owner", "demo", "NORMAL", "input", time.time() + 600)
        self.job = job["id"]
        claim = self.repo.claim(job_id=self.job)
        assert claim
        token = claim["lease_token"]
        for state in (
            "RESEARCHING",
            "PROCESSING",
            "DOWNLOADING",
            "DESIGNING",
            "RENDERING",
            "VALIDATING",
            "DELIVERING",
        ):
            self.repo.transition(self.job, token, state)
        with self.repo.transaction() as db:
            db.execute(
                "CREATE TABLE telegram_outbox(video_sha TEXT PRIMARY KEY, job_id TEXT NOT NULL,"
                " state TEXT NOT NULL, message_id INTEGER, updated_at REAL NOT NULL)"
            )
            db.execute(
                "INSERT INTO telegram_outbox VALUES(?,?,?,?,0)", (SHA, self.job, "PUBLISHED", 7)
            )
            db.execute("INSERT INTO deliveries VALUES(?,?,?)", (SHA, self.job, "PUBLISHED"))
        self.root = Storage(self.config).workspace(self.job)

    def reconcile(self, config: Config, job_id: str, video_sha: str) -> dict[str, Any]:
        return delivery.reconcile_published(config, job_id, video_sha, bot=BOT)


@pytest.fixture
def published(tmp_path: Path) -> Published:
    return Published(tmp_path)


def test_atomic_json_fsyncs_the_directory_after_the_rename(tmp_path: Path) -> None:
    target = tmp_path / "delivery-result.json"
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def replace(a: Any, b: Any) -> None:
        events.append("replace")
        real_replace(a, b)

    def fsync(fd: int) -> None:
        events.append("dir-fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-fsync")
        real_fsync(fd)

    module = "studio_core.delivery.evidence.os"
    with (
        patch(module + ".replace", side_effect=replace),
        patch(module + ".fsync", side_effect=fsync),
    ):
        delivery.atomic_json(target, {"delivery": "PUBLISHED"})
    assert events == ["file-fsync", "replace", "dir-fsync"], "guard:receipt-directory-fsync"
    assert json.loads(target.read_text()) == {"delivery": "PUBLISHED"}
    assert [p.name for p in tmp_path.iterdir()] == ["delivery-result.json"]


def test_atomic_json_failure_leaves_no_partial_receipt(tmp_path: Path) -> None:
    target = tmp_path / "delivery-result.json"
    target.write_text('{"old":true}')
    with (
        patch("studio_core.delivery.evidence.os.replace", side_effect=OSError("disk")),
        pytest.raises(OSError),
    ):
        delivery.atomic_json(target, {"new": True})
    assert json.loads(target.read_text()) == {"old": True}, "guard:receipt-atomic"
    assert [p.name for p in tmp_path.iterdir()] == ["delivery-result.json"], "guard:receipt-atomic"


def test_first_review_mode_wins() -> None:
    db = sqlite3.connect(":memory:")
    delivery.remember_review(db, "job", "sha", "HUMAN_FRAME")
    delivery.remember_review(db, "job", "sha", None)
    rows = db.execute("SELECT review_mode FROM telegram_receipt_metadata").fetchall()
    assert rows == [("HUMAN_FRAME",)], "guard:first-review-durable"
    delivery.remember_review(db, "other", "sha2", None)
    other = db.execute(
        "SELECT review_mode FROM telegram_receipt_metadata WHERE job_id='other'"
    ).fetchone()
    assert other == ("NOT_RECORDED",)


def test_multipart_carries_caption_filename_and_exact_video() -> None:
    body, kind = delivery.multipart(42, b"\x00video\xff", "تعليق", "fact.mp4")
    boundary = kind.split("boundary=")[1]
    assert kind.startswith("multipart/form-data; boundary=studio")
    assert 'name="caption"\r\n\r\nتعليق\r\n'.encode() in body
    assert b'filename="fact.mp4"\r\nContent-Type: video/mp4\r\n\r\n\x00video\xff\r\n' in body
    assert body.endswith(f"--{boundary}--\r\n".encode())


def test_delivery_confirmed_keeps_identity_for_repair() -> None:
    error = delivery.DeliveryConfirmed("job", "sha", "HUMAN_FRAME")
    assert (error.job_id, error.video_sha, error.review_mode) == ("job", "sha", "HUMAN_FRAME")
    assert "reconciliation" in str(error)


def test_reconcile_repairs_evidence_without_any_network(published: Published) -> None:
    with (
        patch.object(socket, "socket", side_effect=AssertionError("network used")),
        patch.object(socket, "create_connection", side_effect=AssertionError("network used")),
    ):
        proof = delivery.reconcile_published(published.config, published.job, SHA, bot=BOT)
    assert (proof["delivery"], proof["bot"], proof["message_id"]) == ("PUBLISHED", BOT, 7)
    assert published.repo.get(published.job)["state"] == "COMPLETED"
    assert json.loads((published.root / "output/delivery-result.json").read_text()) == proof
    assert json.loads((published.root / "manifest.json").read_text())["delivery_receipt"] == proof


def test_manifest_state_is_written_only_when_asked(published: Published) -> None:
    Storage(published.config).manifest(published.job, {"job_id": published.job, "state": "X"})
    delivery.reconcile_published(published.config, published.job, SHA, bot=BOT)
    manifest = json.loads((published.root / "manifest.json").read_text())
    assert manifest["state"] == "X", "Snap keeps its manifest shape"
    delivery.reconcile_published(
        published.config, published.job, SHA, bot=BOT, manifest_state="COMPLETED"
    )
    manifest = json.loads((published.root / "manifest.json").read_text())
    assert manifest["state"] == "COMPLETED", "guard:status-manifest-state"


def test_missing_ack_is_refused(published: Published) -> None:
    with published.repo.transaction() as db:
        db.execute("UPDATE telegram_outbox SET message_id=NULL")
    with pytest.raises(ValueError, match="receipt missing"):
        delivery.reconcile_published(published.config, published.job, SHA, bot=BOT)


def test_sweep_repairs_misaligned_skips_collected_and_bounds_limit(published: Published) -> None:
    calls: list[str] = []

    def reconcile(config: Config, job_id: str, video_sha: str) -> dict[str, Any]:
        calls.append(job_id)
        return published.reconcile(config, job_id, video_sha)

    first = delivery.reconcile_evidence_sweep(published.config, bot=BOT, reconcile=reconcile)
    assert (first["repaired"], calls) == (1, [published.job]), "guard:sweep-repairs"
    again = delivery.reconcile_evidence_sweep(published.config, bot=BOT, reconcile=reconcile)
    assert again["repaired"] == 0, "aligned evidence is left alone"
    (published.root / "output/delivery-result.json").write_text("{}")
    third = delivery.reconcile_evidence_sweep(published.config, bot=BOT, reconcile=reconcile)
    assert third["repaired"] == 1, "guard:sweep-repairs"
    shutil.rmtree(published.root)
    collected = delivery.reconcile_evidence_sweep(published.config, bot=BOT, reconcile=reconcile)
    assert collected["skipped"] == 1, "guard:no-recreate-collected"
    assert not published.root.exists()
    for limit in (0, 101, True, "5"):
        with pytest.raises(ValueError, match="sweep limit"):
            delivery.reconcile_evidence_sweep(
                published.config,
                limit=limit,  # type: ignore[arg-type]
                bot=BOT,
                reconcile=reconcile,
            )


def test_sweep_counts_a_failed_repair_as_pending(published: Published) -> None:
    def broken(config: Config, job_id: str, video_sha: str) -> dict[str, Any]:
        raise OSError("disk")

    result = delivery.reconcile_evidence_sweep(published.config, bot=BOT, reconcile=broken)
    assert result["pending"] == 1, "guard:sweep-pending"


def test_sweep_without_an_outbox_is_empty(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    Repository(config)
    result = delivery.reconcile_evidence_sweep(
        config, bot=BOT, reconcile=lambda *a: pytest.fail("no reconcile without an outbox")
    )
    assert result == {"after": "", "repaired": 0, "pending": 0, "skipped": 0}
