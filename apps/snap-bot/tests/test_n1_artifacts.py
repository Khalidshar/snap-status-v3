import hashlib
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from snap_bot.n1_artifacts import (
    EvidenceConflict,
    Projections,
    Unconfirmed,
    publish_snapshot,
    published_path,
    verified_bytes,
)
from studio_core.config import Config
from studio_core.jobs import LeaseLost, Repository


class Artifacts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = replace(
            Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"), data_root=self.root
        )
        self.repo = Repository(self.cfg)
        self.job, _ = self.repo.enqueue(
            "p0b", "owner", "weather", "NORMAL", "input", self.repo.clock() + 3600
        )
        self.claim = self.repo.claim()
        self.projections = Projections(self.repo)
        self.data = b"reviewed synthetic video"
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.source = self.root / "source.mp4"
        self.source.write_bytes(self.data)

    def body(self, state):
        value = {"job_id": self.job["id"], "state": state}
        if state == "PUBLISHED":
            value.update(video_sha256=self.sha, message_id=7, review_mode="NOT_RECORDED")
        return value

    def advance(self, state):
        return self.projections.advance(
            self.job["id"], state, self.body(state), token=self.claim["lease_token"], attempt=1
        )

    def ack(self):
        with self.repo.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS telegram_outbox(video_sha TEXT PRIMARY KEY,job_id TEXT,state TEXT,message_id INTEGER)"
            )
            db.execute(
                "INSERT OR REPLACE INTO telegram_outbox VALUES(?,?,'PUBLISHED',7)",
                (self.sha, self.job["id"]),
            )
            db.execute(
                "INSERT OR REPLACE INTO deliveries VALUES(?,?,'PUBLISHED')",
                (self.sha, self.job["id"]),
            )

    def test_buffer_is_independent_of_old_open_file_descriptor(self):
        with self.source.open("r+b") as old:
            data = verified_bytes(self.source, self.sha)
            old.seek(0)
            old.write(b"X" * len(self.data))
            old.flush()
            os.fsync(old.fileno())
            target = publish_snapshot(self.root, self.job["id"], 1, data, self.sha)
        self.assertEqual(data, self.data)
        self.assertEqual(target.read_bytes(), self.data)
        self.assertNotEqual(os.stat(self.source).st_ino, os.stat(target).st_ino)

    def test_hash_mismatch_is_rejected(self):
        with self.assertRaises(ValueError, msg="guard:verified buffer hash"):
            verified_bytes(self.source, "f" * 64)

    def test_source_symlink_rejected(self):
        link = self.root / "link.mp4"
        link.symlink_to(self.source)
        with self.assertRaises(ValueError):
            verified_bytes(link, self.sha)

    def test_publication_is_idempotent_without_replacing_inode(self):
        path = publish_snapshot(self.root, self.job["id"], 1, self.data, self.sha)
        inode = path.stat().st_ino
        self.assertEqual(publish_snapshot(self.root, self.job["id"], 1, self.data, self.sha), path)
        self.assertEqual(path.stat().st_ino, inode, msg="guard:create-once inode")
        self.assertEqual(path.stat().st_mode & 0o777, 0o400)

    def test_conflicting_existing_publication_preserved(self):
        path = published_path(self.root, self.job["id"], 1, self.sha)
        path.write_bytes(b"conflict")
        with self.assertRaises(EvidenceConflict):
            publish_snapshot(self.root, self.job["id"], 1, self.data, self.sha)
        self.assertEqual(path.read_bytes(), b"conflict")

    def test_canonical_attempt_path_and_directory_symlink(self):
        for attempt in (True, 0, "01", -1):
            with self.subTest(attempt=attempt):
                with self.assertRaises(ValueError):
                    published_path(self.root, self.job["id"], attempt, self.sha)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.root / "renders").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(ValueError):
            published_path(self.root, self.job["id"], 1, self.sha)

    def test_publication_receipt_uses_existing_core_namespace(self):
        path = publish_snapshot(self.root, self.job["id"], 1, self.data, self.sha)
        relative = path.relative_to(self.root).as_posix()
        self.repo.record_stage(
            self.job["id"], self.claim["lease_token"], 1, "design", self.sha, relative
        )
        receipt = self.repo.verify_stage(
            self.job["id"], "design", self.root, token=self.claim["lease_token"], attempt=1
        )
        self.assertEqual(receipt["path"], relative)

    def test_published_projection_requires_linked_durable_ack(self):
        with self.assertRaises(Unconfirmed, msg="guard:durable ACK"):
            self.advance("PUBLISHED")
        self.ack()
        bad = self.body("PUBLISHED")
        bad["message_id"] = 99
        with self.assertRaises(Unconfirmed):
            self.projections.advance(self.job["id"], "PUBLISHED", bad)
        self.assertEqual(self.advance("PUBLISHED"), self.body("PUBLISHED"))

    def test_published_projection_cannot_regress(self):
        self.advance("AWAITING")
        self.advance("UNKNOWN")
        self.ack()
        self.advance("PUBLISHED")
        self.assertEqual(
            self.advance("AWAITING")["state"], "PUBLISHED", msg="guard:projection rank"
        )
        self.assertEqual(self.advance("UNKNOWN")["state"], "PUBLISHED")

    def test_same_rank_conflicting_receipt_refused(self):
        self.ack()
        self.advance("PUBLISHED")
        bad = self.body("PUBLISHED")
        bad["review_mode"] = "HUMAN_FRAME"
        with self.assertRaises(EvidenceConflict):
            self.projections.advance(self.job["id"], "PUBLISHED", bad)

    def test_stale_owner_cannot_write_initial_projection(self):
        with self.assertRaises(LeaseLost):
            self.projections.advance(
                self.job["id"], "AWAITING", self.body("AWAITING"), token="stale", attempt=1
            )

    def test_failed_materialization_can_be_repaired_from_db(self):
        destination = self.root / "manifest.json"
        self.advance("AWAITING")
        self.projections.materialize(self.job["id"], destination)
        self.ack()
        self.advance("PUBLISHED")

        def fail():
            raise OSError("synthetic replace fault")

        with self.assertRaises(OSError):
            self.projections.materialize(self.job["id"], destination, before_replace=fail)
        self.assertEqual(json.loads(destination.read_text())["state"], "AWAITING")
        self.assertEqual(self.projections.read(self.job["id"])["state"], "PUBLISHED")
        self.projections.materialize(self.job["id"], destination)
        self.assertEqual(json.loads(destination.read_text())["state"], "PUBLISHED")

    def test_old_materializer_cannot_overwrite_new_published_view(self):
        destination = self.root / "manifest.json"
        ready = threading.Event()
        resume = threading.Event()
        errors = []
        self.advance("AWAITING")

        def barrier():
            ready.set()
            resume.wait(5)

        def materialize(callback=None):
            try:
                self.projections.materialize(self.job["id"], destination, before_replace=callback)
            except Exception as error:
                errors.append(type(error).__name__)

        old = threading.Thread(target=materialize, args=(barrier,))
        old.start()
        self.assertTrue(ready.wait(5))
        self.ack()
        self.advance("PUBLISHED")
        new = threading.Thread(target=materialize)
        new.start()
        # The database remains writable while the old projection holds its FILE lock.
        self.repo.heartbeat(self.job["id"], self.claim["lease_token"])
        resume.set()
        old.join(5)
        new.join(5)
        self.assertFalse(old.is_alive())
        self.assertFalse(new.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(destination.read_text())["state"], "PUBLISHED")


if __name__ == "__main__":
    unittest.main()
