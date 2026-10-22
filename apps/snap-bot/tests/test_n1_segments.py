import multiprocessing
import os
import signal
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from snap_bot.n1_contracts import Refused
from snap_bot.n1_segments import JournalSegments, SegmentCorrupt
from studio_core.config import Config
from studio_core.jobs import LeaseLost, Repository


class Segments(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = replace(
            Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"),
            data_root=Path(self.temp.name) / "data",
        )
        self.repo = Repository(self.cfg)
        self.job, _ = self.repo.enqueue(
            "segment", "owner", "weather", "NORMAL", "input", self.repo.clock() + 3600
        )
        self.claim = self.repo.claim()
        self.key = b"p0-segment-synthetic-key-000000000000"
        self.root = Path(self.temp.name) / "journal"
        self.j = JournalSegments(self.repo, self.key, self.root)

    def begin(self, event="start"):
        return self.j.begin(
            self.job["id"],
            self.claim["lease_token"],
            self.claim["attempt"],
            "fact_source",
            "RUNNING",
            event,
            "a" * 64,
        )

    def confirm(self, receipt):
        return self.j.confirm(
            self.job["id"], self.claim["lease_token"], self.claim["attempt"], receipt
        )

    def test_real_append_fsync_confirm_and_pending_visibility(self):
        env = self.begin()
        receipt = self.j.append(env)
        self.assertEqual(self.j.accepted(self.job["id"]), [], msg="guard:ledger authority view")
        self.assertEqual(self.j.append(env), receipt)
        self.confirm(receipt)
        self.assertEqual(self.j.accepted(self.job["id"]), [receipt])
        self.assertEqual(self.j.scan(env["body"]["gen"]), [receipt])

    def test_old_live_writer_finishes_only_in_sealed_generation(self):
        env = self.begin()
        ready = threading.Event()
        resume = threading.Event()
        outcome = []
        errors = []

        def pause():
            ready.set()
            resume.wait(5)

        def old():
            try:
                outcome.append(self.j.append(env, before_write=pause))
            except Exception as error:
                errors.append(type(error).__name__)

        thread = threading.Thread(target=old)
        thread.start()
        self.assertTrue(ready.wait(5))
        try:
            newgen = self.j.seal(env["body"]["gen"])
            fresh = self.begin("new")
            self.assertEqual(fresh["body"]["gen"], newgen)
            confirmed = self.j.append(fresh)
            self.confirm(confirmed)
            self.repo.heartbeat(self.job["id"], self.claim["lease_token"])
        finally:
            resume.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        with self.assertRaises(Refused):
            self.confirm(outcome[0])
        self.assertEqual(self.j.accepted(self.job["id"]), [confirmed])
        self.assertTrue(Path(str(self.j.path(env["body"]["gen"])) + ".lock").exists())
        self.assertEqual(self.j.scan(env["body"]["gen"]), [])

    def test_partial_tail_is_retained_and_ignored_after_seal(self):
        env = self.begin()
        receipt = self.j.append(env)
        self.confirm(receipt)
        path = self.j.path(env["body"]["gen"])
        with path.open("ab") as stream:
            stream.write(b'{"unfinished":')
            stream.flush()
            os.fsync(stream.fileno())
        before = path.read_bytes()
        with self.assertRaises(SegmentCorrupt):
            self.j.scan(env["body"]["gen"])
        new = self.j.seal(env["body"]["gen"])
        self.assertEqual(path.read_bytes(), before)
        try:
            accepted = self.j.scan(env["body"]["gen"])
        except SegmentCorrupt:
            self.fail("guard:sealed prefix bound: sealed scan consumed the retained partial tail")
        self.assertEqual(accepted, [receipt], msg="guard:sealed prefix bound")
        self.assertEqual(self.j.metadata(new)["base_digest"], receipt["digest"])
        self.assertEqual(self.j.accepted(self.job["id"]), [receipt])

    def test_crash_after_logical_seal_is_recoverable(self):
        env = self.begin()

        def fail(point):
            if point == "after_logical_seal":
                raise OSError("synthetic crash")

        with self.assertRaises(OSError):
            self.j.seal(env["body"]["gen"], failpoint=fail)
        with self.repo.connect() as db:
            new = db.execute("SELECT gen FROM n1_segments WHERE state='ACTIVE'").fetchone()[0]
        self.assertEqual(self.j.metadata(new)["phase"], "PREPARING", msg="guard:durable readiness")
        raw = self.j.ledger.begin(
            self.job["id"], self.claim["lease_token"], 1, "fact_source", "RUNNING", "next", "a" * 64
        )
        with self.assertRaises(Refused):
            self.j.append(raw)
        restored = JournalSegments(self.repo, self.key, self.root)
        self.assertEqual(restored.metadata(new)["phase"], "READY")
        receipt = restored.append(raw)
        restored.confirm(self.job["id"], self.claim["lease_token"], 1, receipt)
        self.assertEqual(restored.accepted(self.job["id"]), [receipt])

    def test_crash_after_file_publish_is_idempotently_recovered(self):
        env = self.begin()

        def fail(point):
            if point == "after_file_publish":
                raise OSError("synthetic crash")

        with self.assertRaises(OSError):
            self.j.seal(env["body"]["gen"], failpoint=fail)
        with self.repo.connect() as db:
            new = db.execute("SELECT gen FROM n1_segments WHERE state='ACTIVE'").fetchone()[0]
        inode = self.j.path(new).stat().st_ino
        restored = JournalSegments(self.repo, self.key, self.root)
        self.assertEqual(restored.metadata(new)["phase"], "READY")
        self.assertEqual(restored.path(new).stat().st_ino, inode)
        self.assertEqual(restored.seal(env["body"]["gen"]), new)

    def test_physical_tamper_refuses_accepted_view(self):
        env = self.begin()
        receipt = self.j.append(env)
        self.confirm(receipt)
        path = self.j.path(env["body"]["gen"])
        data = path.read_bytes().replace(
            b'"payload_sha256":"' + b"a" * 64 + b'"', b'"payload_sha256":"' + b"b" * 64 + b'"'
        )
        path.write_bytes(data)
        with self.assertRaises(SegmentCorrupt):
            self.j.accepted(self.job["id"])

    def test_foreign_receipt_cannot_be_adopted_without_physical_presence(self):
        env = self.begin()
        receipt = self.j.ledger.simulated_writer_receipt(env)
        with self.assertRaises(Refused, msg="guard:physical presence"):
            self.confirm(receipt)
        self.assertEqual(self.j.accepted(self.job["id"]), [])

    def test_old_owner_cannot_confirm_physically_present_receipt(self):
        env = self.begin()
        receipt = self.j.append(env)
        with self.repo.transaction() as db:
            db.execute(
                "UPDATE jobs SET lease_until=? WHERE id=?", (self.repo.clock() - 1, self.job["id"])
            )
        self.repo.claim(job_id=self.job["id"])
        with self.assertRaises(LeaseLost):
            self.confirm(receipt)
        self.assertEqual(self.j.accepted(self.job["id"]), [])

    def test_confirmed_prefix_anchor_and_child_header_bind_parent(self):
        env = self.begin()
        receipt = self.j.append(env)
        self.confirm(receipt)
        new = self.j.seal(env["body"]["gen"])
        meta = self.j.metadata(new)
        self.assertEqual(meta["parent"], env["body"]["gen"])
        self.assertEqual(meta["base_seq"], 1)
        with self.j.path(new).open("wb") as stream:
            stream.write(b"{}\n")
        with self.assertRaises(SegmentCorrupt):
            self.j.scan(new)

    @unittest.skipUnless(os.name == "posix", "Linux process fixture")
    def test_sigkill_after_fsync_preserves_orphan_and_lock(self):
        env = self.begin()
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        hold = context.Event()

        def child():
            def after():
                ready.set()
                hold.wait(10)

            self.j.append(env, after_fsync=after)

        process = context.Process(target=child)
        process.start()
        try:
            self.assertTrue(ready.wait(5))
            os.kill(process.pid, signal.SIGKILL)
            process.join(5)
            self.assertEqual(process.exitcode, -signal.SIGKILL)
            oldpath = self.j.path(env["body"]["gen"])
            before = oldpath.read_bytes()
            self.j.seal(env["body"]["gen"])
            self.assertEqual(oldpath.read_bytes(), before)
            self.assertTrue(Path(str(oldpath) + ".lock").exists())
            self.assertEqual(self.j.accepted(self.job["id"]), [])
        finally:
            # Never acquire a multiprocessing condition after SIGKILL: the child
            # may have died while owning its semaphore. Process death is the proof.
            if process.is_alive():
                process.kill()
                process.join(5)


if __name__ == "__main__":
    unittest.main()
