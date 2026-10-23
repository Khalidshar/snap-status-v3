import hashlib
import json
import multiprocessing
import os
import signal
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from n1_spawn_targets import killed_partial

from snap_bot import n1_partial_tail
from snap_bot.n1_contracts import Ledger
from snap_bot.n1_control import ControlClient, ControlHandler, ControlService
from snap_bot.n1_coordinator import JournalCoordinator
from snap_bot.n1_generations import RegisteredWriter
from snap_bot.n1_writer import PhysicalWriter, WriterRefused, canonical, generation, mac
from studio_core.config import Config
from studio_core.jobs import LeaseLost, Repository


@unittest.skipUnless(os.name == "posix", "Linux partial-write recovery")
class PartialTail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = b"partial-tail-synthetic-key-00000000000"
        self.physical = PhysicalWriter(self.root / "journal", self.key, "snap-bot-v2")
        self.writer = RegisteredWriter(self.physical)
        self.path = self.root / "control.sock"
        self.service = ControlService(
            self.path, os.getuid(), "snap-bot-v2", ControlHandler(self.writer.authority)
        )
        self.service.start()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.service.run, args=(self.stop,))
        self.thread.start()
        self.client = ControlClient(self.path, os.getuid(), "snap-bot-v2")
        cfg = replace(
            Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"),
            data_root=self.root / "data",
        )
        self.repo = Repository(cfg)
        self.job, _ = self.repo.enqueue(
            "tail", "owner", "weather", "NORMAL", "input", self.repo.clock() + 3600
        )
        self.owner = self.repo.claim()
        self.ledger = Ledger(self.repo, self.key)
        self.coordinator = JournalCoordinator(self.ledger, self.client)

    def tearDown(self):
        self.stop.set()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.service.shutdown(3)

    def args(self):
        return (self.job["id"], self.owner["lease_token"], self.owner["attempt"])

    def begin(self, event):
        return self.coordinator.begin(*self.args(), "fact_source", "RUNNING", event, "a" * 64)

    def append(self, body):
        return self.writer.append(body["generation"], body["envelope"])

    def confirm(self, receipt):
        self.ledger.confirm(*self.args(), receipt)

    def source(self, body):
        return self.physical.path(body["generation"]["body"])

    def audit(self, body):
        return self.physical.root / ("tail-" + body["generation"]["body"]["gen"] + ".json")

    def fault(self, path, raw):
        process = multiprocessing.get_context("spawn").Process(
            target=killed_partial, args=(path, raw)
        )
        process.start()
        try:
            process.join(5)
            self.assertEqual(process.exitcode, -signal.SIGKILL)
        finally:
            if process.is_alive():
                process.kill()
                process.join(3)

    def first_and_tail(self, orphan=False):
        first = self.begin("first")
        receipt = self.append(first)
        self.confirm(receipt)
        pending = self.begin("pending")
        if orphan:
            tip = self.append(pending)
            # Produce a signed physical orphan without adopting it into the DB.
            env = dict(pending["envelope"])
            body = dict(env["body"])
            body.update(intent_id="f" * 32, seq=3, prev=tip["digest"])
            env = {"body": body, "mac": mac(self.key, "n1.intent.v1", body)}
        else:
            env = pending["envelope"]
        model = self.ledger.simulated_writer_receipt(env)
        self.fault(self.source(first), (canonical(model) + "\n").encode()[:137])
        return first, receipt, pending

    def test_sigkill_partial_record_rotates_without_adoption_or_truncation(self):
        first, receipt, pending = self.first_and_tail()
        path = self.source(first)
        before = path.read_bytes()
        with self.assertRaisesRegex(WriterRefused, "PARTIAL_SEGMENT"):
            self.append(pending)
        child = self.coordinator.rotate(*self.args())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            (child["body"]["base_seq"], child["body"]["base_digest"]), (1, receipt["digest"])
        )
        self.assertTrue(self.audit(first).exists())
        audit = json.loads(self.audit(first).read_bytes())
        body = audit["body"]
        self.assertEqual(body["kind"], "PARTIAL_RECORD")
        self.assertEqual(body["prefix_seq"], 1)
        self.assertEqual(body["segment_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(body["tail_sha256"], hashlib.sha256(before[body["offset"] :]).hexdigest())
        self.assertEqual(body["tail_bytes"], 137)
        self.assertEqual(audit["proof"], mac(self.key, "n1.partial-tail.v1", body))
        with self.repo.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT state FROM n1_ledger WHERE intent=?",
                    (pending["envelope"]["body"]["intent_id"],),
                ).fetchone()[0],
                "UNKNOWN",
            )
        with self.assertRaisesRegex(WriterRefused, "GENERATION_SEALED"):
            self.append(pending)
        next_receipt = self.append(self.begin("next"))
        self.confirm(next_receipt)
        self.assertEqual(self.ledger.view(self.job["id"]), [receipt, next_receipt])

    def test_physical_orphan_and_partial_tail_do_not_change_confirmed_child_anchor(self):
        first, receipt, _pending = self.first_and_tail(orphan=True)
        before = self.source(first).read_bytes()
        child = self.coordinator.rotate(*self.args())
        audit = json.loads(self.audit(first).read_bytes())
        self.assertEqual(audit["body"]["prefix_seq"], 2)
        self.assertEqual(child["body"]["base_seq"], 1)
        next_receipt = self.append(self.begin("next"))
        self.confirm(next_receipt)
        self.assertEqual(next_receipt["envelope"]["body"]["seq"], 2)
        self.assertEqual(self.ledger.view(self.job["id"]), [receipt, next_receipt])
        self.assertEqual(self.source(first).read_bytes(), before)

    def test_partial_header_empty_and_prefix_can_be_sealed_without_events(self):
        for raw in (b"", None):
            with self.subTest(raw=raw):
                desc = self.coordinator.ensure_current(*self.args())
                path = self.physical.path(desc["body"])
                fragment = b"" if raw == b"" else (canonical(desc["body"]) + "\n").encode()[:51]
                path.write_bytes(fragment)
                path.chmod(0o600)
                child = self.coordinator.rotate(*self.args())
                self.assertEqual(child["body"]["base_seq"], 0)
                self.assertEqual(path.read_bytes(), fragment)
                audit = json.loads(
                    (self.physical.root / ("tail-" + desc["body"]["gen"] + ".json")).read_bytes()
                )
                self.assertEqual(audit["body"]["kind"], "PARTIAL_HEADER")
                self.assertEqual(audit["body"]["tail_bytes"], len(fragment))

    def test_default_scan_stays_strict_after_authority_prefix_inspection(self):
        first, _, _ = self.first_and_tail()
        reports = []
        self.assertEqual(
            len(
                self.physical.scan(
                    self.source(first), first["generation"]["body"], partial_tail=reports
                )
            ),
            1,
        )
        self.assertEqual(len(reports), 1)
        with self.assertRaisesRegex(WriterRefused, "PARTIAL_SEGMENT"):
            self.physical.scan(self.source(first), first["generation"]["body"])
        self.assertFalse(self.audit(first).exists())

    def test_complete_corruption_and_oversized_tail_refuse_seal(self):
        first = self.begin("first")
        self.append(first)
        path = self.source(first)
        good = path.read_bytes()
        for bad in (b"{}\n", b"x" * 8193):
            with self.subTest(bad=bad[:8]):
                path.write_bytes(good + bad)
                child = generation(
                    {
                        **first["generation"]["body"],
                        "gen": "f" * 32,
                        "parent": first["generation"]["body"]["gen"],
                    },
                    self.key,
                )
                with self.assertRaises(WriterRefused):
                    self.writer.authority.seal(first["generation"]["body"]["gen"], child)
                self.assertEqual(
                    self.writer.authority.load()["generations"][first["generation"]["body"]["gen"]][
                        "state"
                    ],
                    "ACTIVE",
                )
                self.assertFalse(self.audit(first).exists())
                self.assertEqual(path.read_bytes(), good + bad)

    def test_nonprefix_and_complete_bad_header_are_not_partial_writes(self):
        desc = self.coordinator.ensure_current(*self.args())
        path = self.physical.path(desc["body"])
        child = generation(
            {**desc["body"], "gen": "f" * 32, "parent": desc["body"]["gen"]}, self.key
        )
        for raw in (b"foreign", b"{}\n"):
            path.write_bytes(raw)
            path.chmod(0o600)
            with self.assertRaisesRegex(WriterRefused, "GENERATION_CONFLICT"):
                self.writer.authority.seal(desc["body"]["gen"], child)
            self.assertFalse(
                (self.physical.root / ("tail-" + desc["body"]["gen"] + ".json")).exists()
            )

    def test_unconfirmed_partial_anchor_is_rejected_before_audit_write(self):
        first, _, pending = self.first_and_tail()
        model = self.ledger.simulated_writer_receipt(pending["envelope"])
        child = generation(
            {
                **first["generation"]["body"],
                "gen": "f" * 32,
                "parent": first["generation"]["body"]["gen"],
                "base_seq": 2,
                "base_digest": model["digest"],
            },
            self.key,
        )
        with self.assertRaisesRegex(WriterRefused, "SEAL_ANCHOR"):
            self.writer.authority.seal(first["generation"]["body"]["gen"], child)
        self.assertFalse(self.audit(first).exists())

    def test_audit_failure_leaves_physical_parent_active_and_stable_pending_child(self):
        first, _, _ = self.first_and_tail()
        with patch("snap_bot.n1_generations.retain", side_effect=OSError("synthetic retention")):
            with self.assertRaises(Exception):
                self.coordinator.rotate(*self.args())
        self.assertEqual(
            self.writer.authority.load()["generations"][first["generation"]["body"]["gen"]][
                "state"
            ],
            "ACTIVE",
        )
        with self.repo.connect() as db:
            child = db.execute("SELECT gen FROM n1_segments WHERE state='ACTIVE'").fetchone()[0]
            self.assertEqual(db.execute("SELECT COUNT(*) FROM n1_segments").fetchone()[0], 2)
        fresh = self.coordinator.ensure_current(*self.args())
        self.assertEqual(fresh["body"]["gen"], child)
        self.assertTrue(self.audit(first).exists())

    def test_crash_after_audit_retries_same_child_and_same_immutable_audit(self):
        first, _, _ = self.first_and_tail()
        original = n1_partial_tail.retain

        def lost(*args):
            original(*args)
            raise OSError("synthetic crash before registry seal")

        with patch("snap_bot.n1_generations.retain", side_effect=lost):
            with self.assertRaises(Exception):
                self.coordinator.rotate(*self.args())
        path = self.audit(first)
        before = path.read_bytes()
        stamp = path.stat().st_mtime_ns
        self.assertEqual(
            self.writer.authority.load()["generations"][first["generation"]["body"]["gen"]][
                "state"
            ],
            "ACTIVE",
        )
        with self.repo.connect() as db:
            child = db.execute("SELECT gen FROM n1_segments WHERE state='ACTIVE'").fetchone()[0]
        self.assertEqual(self.coordinator.ensure_current(*self.args())["body"]["gen"], child)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_conflicting_or_public_audit_is_never_overwritten(self):
        first, _, _ = self.first_and_tail()
        path = self.audit(first)
        path.write_bytes(b"unchanged")
        path.chmod(0o600)
        for mode in (0o600, 0o644):
            path.chmod(mode)
            with self.assertRaises(Exception):
                self.coordinator.rotate(
                    *self.args()
                ) if mode == 0o600 else self.coordinator.ensure_current(*self.args())
            self.assertEqual(path.read_bytes(), b"unchanged")
            self.assertEqual(
                self.writer.authority.load()["generations"][first["generation"]["body"]["gen"]][
                    "state"
                ],
                "ACTIVE",
            )

    def test_symlink_audit_cannot_replace_outside_file(self):
        first, _, _ = self.first_and_tail()
        outside = self.root / "outside"
        outside.write_bytes(b"unchanged")
        self.audit(first).symlink_to(outside)
        with self.assertRaises(Exception):
            self.coordinator.rotate(*self.args())
        self.assertTrue(self.audit(first).is_symlink())
        self.assertEqual(outside.read_bytes(), b"unchanged")
        self.assertEqual(
            self.writer.authority.load()["generations"][first["generation"]["body"]["gen"]][
                "state"
            ],
            "ACTIVE",
        )

    def test_expired_owner_cannot_seal_or_write_audit(self):
        first, _, _ = self.first_and_tail()
        before = self.source(first).read_bytes()
        with self.repo.transaction() as db:
            db.execute(
                "UPDATE jobs SET lease_until=? WHERE id=?", (self.repo.clock() - 1, self.job["id"])
            )
        with self.assertRaises(LeaseLost):
            self.coordinator.rotate(*self.args())
        self.assertFalse(self.audit(first).exists())
        self.assertEqual(self.source(first).read_bytes(), before)

    def test_tail_receipt_fsync_failure_is_not_a_seal_ack(self):
        first, _, _ = self.first_and_tail()
        with (
            patch(
                "snap_bot.n1_partial_tail.sync_dir",
                side_effect=OSError("synthetic directory durability"),
            ),
            self.assertRaises(Exception),
        ):
            self.coordinator.rotate(*self.args())
        self.assertTrue(self.audit(first).exists())
        self.assertEqual(
            self.writer.authority.load()["generations"][first["generation"]["body"]["gen"]][
                "state"
            ],
            "ACTIVE",
        )
        self.coordinator.ensure_current(*self.args())
        self.assertEqual(
            self.writer.authority.load()["generations"][first["generation"]["body"]["gen"]][
                "state"
            ],
            "SEALED",
        )


if __name__ == "__main__":
    unittest.main()
