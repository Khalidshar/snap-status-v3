import copy
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from snap_bot.n1_contracts import Ledger, Refused
from snap_bot.n1_writer import IsolatedAppendHandler, PhysicalWriter, WriterRefused, generation
from studio_core.config import Config
from studio_core.jobs import LeaseLost, Repository


@unittest.skipUnless(os.name == "posix", "Linux filesystem writer")
class Writer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = b"writer-synthetic-key-00000000000000000"
        cfg = replace(
            Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"),
            data_root=self.root / "data",
        )
        self.repo = Repository(cfg)
        self.job, _ = self.repo.enqueue(
            "writer", "owner", "weather", "NORMAL", "input", self.repo.clock() + 3600
        )
        self.owner = self.repo.claim()
        self.ledger = Ledger(self.repo, self.key)
        self.writer = PhysicalWriter(self.root / "journal", self.key, cfg.app)
        self.env = self.begin("first")
        self.desc = self.describe(self.env["body"]["gen"])

    def begin(self, event):
        return self.ledger.begin(
            self.job["id"],
            self.owner["lease_token"],
            self.owner["attempt"],
            "fact_source",
            "RUNNING",
            event,
            "a" * 64,
        )

    def describe(self, gen, parent=None):
        with self.repo.connect() as db:
            row = db.execute("SELECT * FROM n1_segments WHERE gen=?", (gen,)).fetchone()
        return generation(
            {
                "type": "segment",
                "app": self.repo.config.app,
                "gen": gen,
                "parent": parent,
                "base_seq": row["seq"],
                "base_digest": row["digest"],
            },
            self.key,
        )

    def confirm(self, receipt):
        return self.ledger.confirm(
            self.job["id"], self.owner["lease_token"], self.owner["attempt"], receipt
        )

    def test_durable_receipt_adopted_by_db_authority_without_writer_db(self):
        receipt = self.writer.append(self.desc, self.env)
        self.assertEqual(self.ledger.view(self.job["id"]), [])
        self.confirm(receipt)
        self.assertEqual(self.ledger.view(self.job["id"]), [receipt])
        self.assertEqual(
            self.writer.scan(self.writer.path(self.desc["body"]), self.desc["body"]), [receipt]
        )

    def test_duplicate_intent_never_appends_again(self):
        receipt = self.writer.append(self.desc, self.env)
        path = self.writer.path(self.desc["body"])
        before = path.read_bytes()
        self.assertEqual(self.writer.append(self.desc, self.env), receipt)
        self.assertEqual(path.read_bytes(), before)

    def test_unsigned_generation_rejected_before_any_file(self):
        self.desc["mac"] = "0" * 64
        with self.assertRaisesRegex(WriterRefused, "GENERATION_MAC"):
            self.writer.append(self.desc, self.env)
        self.assertEqual(list(self.writer.root.iterdir()), [])

    def test_wrong_app_is_rejected_even_with_valid_signature(self):
        body = dict(self.desc["body"], app="status-bot-v2")
        signed = generation(body, self.key)
        with self.assertRaisesRegex(WriterRefused, "GENERATION_BINDING"):
            self.writer.append(signed, self.env)

    def test_wrong_intent_mac_is_rejected(self):
        env = copy.deepcopy(self.env)
        env["mac"] = "0" * 64
        with self.assertRaisesRegex(WriterRefused, "INTENT_MAC"):
            self.writer.append(self.desc, env)

    def test_expired_intent_cannot_write(self):
        self.writer.clock = lambda: self.env["body"]["not_after"]
        with self.assertRaisesRegex(WriterRefused, "INTENT_EXPIRED"):
            self.writer.append(self.desc, self.env)
        self.assertEqual(list(self.writer.root.iterdir()), [])

    def test_partial_tail_retained_and_not_overwritten(self):
        self.writer.append(self.desc, self.env)
        path = self.writer.path(self.desc["body"])
        with path.open("ab") as stream:
            stream.write(b"{partial")
        before = path.read_bytes()
        with self.assertRaisesRegex(WriterRefused, "PARTIAL_SEGMENT"):
            self.writer.append(self.desc, self.env)
        self.assertEqual(path.read_bytes(), before)

    def test_generation_descriptor_conflict_refused(self):
        self.writer.append(self.desc, self.env)
        body = dict(self.desc["body"], parent="b" * 32)
        with self.assertRaisesRegex(WriterRefused, "GENERATION_CONFLICT"):
            self.writer.append(generation(body, self.key), self.env)

    def test_symlink_segment_is_refused(self):
        outside = self.root / "outside"
        outside.write_bytes(b"untouched")
        self.writer.path(self.desc["body"]).symlink_to(outside)
        with self.assertRaises(OSError):
            self.writer.append(self.desc, self.env)
        self.assertEqual(outside.read_bytes(), b"untouched")

    def test_symlink_lock_is_refused(self):
        outside = self.root / "outside"
        outside.write_bytes(b"untouched")
        Path(str(self.writer.path(self.desc["body"])) + ".lock").symlink_to(outside)
        with self.assertRaises(OSError):
            self.writer.append(self.desc, self.env)
        self.assertEqual(outside.read_bytes(), b"untouched")

    def test_old_physical_receipt_cannot_be_adopted_after_reclaim(self):
        receipt = self.writer.append(self.desc, self.env)
        with self.repo.transaction() as db:
            db.execute(
                "UPDATE jobs SET lease_until=? WHERE id=?", (self.repo.clock() - 1, self.job["id"])
            )
        self.repo.claim(job_id=self.job["id"])
        with self.assertRaises(LeaseLost):
            self.confirm(receipt)
        self.assertEqual(self.ledger.view(self.job["id"]), [])

    def test_sealed_orphan_does_not_block_new_generation(self):
        receipt = self.writer.append(self.desc, self.env)
        old = self.env["body"]["gen"]
        before = self.writer.path(self.desc["body"]).read_bytes()
        new = self.ledger.seal(old)
        with self.assertRaises(Refused):
            self.confirm(receipt)
        fresh = self.begin("new")
        desc = self.describe(new, parent=old)
        result = self.writer.append(desc, fresh)
        self.confirm(result)
        self.assertEqual(self.ledger.view(self.job["id"]), [result])
        self.assertEqual(self.writer.path(self.desc["body"]).read_bytes(), before)

    def test_child_generation_carries_confirmed_prefix_anchor(self):
        receipt = self.writer.append(self.desc, self.env)
        self.confirm(receipt)
        old = self.env["body"]["gen"]
        new = self.ledger.seal(old)
        fresh = self.begin("child")
        desc = self.describe(new, parent=old)
        self.assertEqual(desc["body"]["base_digest"], receipt["digest"])
        result = self.writer.append(desc, fresh)
        self.confirm(result)
        self.assertEqual(result["envelope"]["body"]["seq"], 2)

    def test_handler_rejects_extra_or_missing_control_fields(self):
        handler = IsolatedAppendHandler(self.writer)
        for request in (
            {},
            {"generation": self.desc, "envelope": self.env, "db_path": "forbidden"},
        ):
            with self.assertRaises(WriterRefused):
                handler(request)

    def test_world_readable_writer_root_is_rejected(self):
        root = self.root / "unsafe"
        root.mkdir()
        root.chmod(0o755)
        with self.assertRaisesRegex(WriterRefused, "UNSAFE_JOURNAL_ROOT"):
            PhysicalWriter(root, self.key, self.repo.config.app)

    def test_fsync_failure_never_returns_receipt_and_retry_syncs_again(self):
        actual = os.fsync
        path = self.writer.path(self.desc["body"])
        calls = []

        def fail_append(fd):
            if os.readlink("/proc/self/fd/" + str(fd)) == str(path):
                calls.append(fd)
                raise OSError("synthetic append fsync failure")
            return actual(fd)

        with patch("snap_bot.n1_writer.os.fsync", side_effect=fail_append):
            with self.assertRaises(OSError):
                self.writer.append(self.desc, self.env)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.ledger.view(self.job["id"]), [])
        # Retry has a complete line, but it still must fsync before receipt.
        with patch("snap_bot.n1_writer.os.fsync", side_effect=fail_append):
            with self.assertRaises(OSError):
                self.writer.append(self.desc, self.env)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.ledger.view(self.job["id"]), [])
        receipt = self.writer.append(self.desc, self.env)
        self.confirm(receipt)
        self.assertEqual(self.ledger.view(self.job["id"]), [receipt])


if __name__ == "__main__":
    unittest.main()
