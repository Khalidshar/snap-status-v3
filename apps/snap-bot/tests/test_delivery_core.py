"""Shared delivery evidence in the core (0.11.0): receipts and network-free repair."""

import json
import os
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

from test_telegram_delivery import DeliveryTests

from snap_bot.telegram_egress import EXPECTED_BOT
from studio_core import delivery


class ReceiptPrimitives(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_atomic_json_fsyncs_the_directory_after_the_rename(self):
        target = self.root / "delivery-result.json"
        events = []
        real_fsync = os.fsync
        real_replace = os.replace

        def replace(a, b):
            events.append("replace")
            return real_replace(a, b)

        def fsync(fd):
            import stat

            events.append("dir-fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-fsync")
            return real_fsync(fd)

        with (
            patch("studio_core.delivery.evidence.os.replace", side_effect=replace),
            patch("studio_core.delivery.evidence.os.fsync", side_effect=fsync),
        ):
            delivery.atomic_json(target, {"delivery": "PUBLISHED"})
        self.assertEqual(
            events, ["file-fsync", "replace", "dir-fsync"], "guard:receipt-directory-fsync"
        )
        self.assertEqual(json.loads(target.read_text()), {"delivery": "PUBLISHED"})
        self.assertEqual(
            [p.name for p in self.root.iterdir()],
            ["delivery-result.json"],
            "no temporary receipt left",
        )

    def test_atomic_json_failure_leaves_no_partial_receipt(self):
        target = self.root / "delivery-result.json"
        target.write_text('{"old":true}')
        with (
            patch("studio_core.delivery.evidence.os.replace", side_effect=OSError("disk")),
            self.assertRaises(OSError),
        ):
            delivery.atomic_json(target, {"new": True})
        self.assertEqual(json.loads(target.read_text()), {"old": True}, "guard:receipt-atomic")
        self.assertEqual(
            [p.name for p in self.root.iterdir()], ["delivery-result.json"], "guard:receipt-atomic"
        )

    def test_first_review_mode_wins(self):
        import sqlite3

        db = sqlite3.connect(":memory:")
        delivery.remember_review(db, "job", "sha", "HUMAN_FRAME")
        delivery.remember_review(db, "job", "sha", None)
        self.assertEqual(
            db.execute("SELECT review_mode FROM telegram_receipt_metadata").fetchall(),
            [("HUMAN_FRAME",)],
            "guard:first-review-durable",
        )
        delivery.remember_review(db, "other", "sha2", None)
        self.assertEqual(
            db.execute(
                "SELECT review_mode FROM telegram_receipt_metadata WHERE job_id='other'"
            ).fetchone(),
            ("NOT_RECORDED",),
        )

    def test_multipart_carries_caption_filename_and_exact_video(self):
        body, kind = delivery.multipart(42, b"\x00video\xff", "تعليق", "fact.mp4")
        boundary = kind.split("boundary=")[1]
        self.assertTrue(kind.startswith("multipart/form-data; boundary=studio"))
        self.assertIn('name="caption"\r\n\r\nتعليق\r\n'.encode(), body)
        self.assertIn(
            b'filename="fact.mp4"\r\nContent-Type: video/mp4\r\n\r\n\x00video\xff\r\n', body
        )
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))

    def test_delivery_confirmed_keeps_identity_for_repair(self):
        error = delivery.DeliveryConfirmed("job", "sha", "HUMAN_FRAME")
        self.assertEqual(
            (error.job_id, error.video_sha, error.review_mode), ("job", "sha", "HUMAN_FRAME")
        )
        self.assertIn("reconciliation", str(error))


class ReconcileFromDurableAck(DeliveryTests):
    """Reuse the real Snap delivery fixture, then repair through the core only."""

    def publish(self):
        from snap_bot.telegram_egress import deliver

        self.assertEqual(
            deliver(self.config, self.job, self.digest, self.transport)["delivery"], "PUBLISHED"
        )
        return self.config.data_root / "renders" / self.job

    def test_reconcile_repairs_evidence_without_any_network(self):
        root = self.publish()
        for name in ("output/delivery-result.json",):
            (root / name).unlink()
        manifest = json.loads((root / "manifest.json").read_text())
        manifest.pop("delivery_receipt")
        (root / "manifest.json").write_text(json.dumps(manifest))
        with (
            patch.object(socket, "socket", side_effect=AssertionError("network used")),
            patch.object(socket, "create_connection", side_effect=AssertionError("network used")),
        ):
            proof = delivery.reconcile_published(
                self.config, self.job, self.digest, bot=EXPECTED_BOT
            )
        self.assertEqual(
            (proof["delivery"], proof["bot"], proof["message_id"]), ("PUBLISHED", EXPECTED_BOT, 7)
        )
        self.assertEqual(json.loads((root / "output/delivery-result.json").read_text()), proof)
        self.assertEqual(
            json.loads((root / "manifest.json").read_text())["delivery_receipt"], proof
        )
        self.assertEqual(self.calls.count("sendVideo"), 1, "guard:never-resend")

    def test_manifest_state_is_written_only_when_asked(self):
        root = self.publish()
        manifest = json.loads((root / "manifest.json").read_text())
        manifest["state"] = "DELIVERING"
        (root / "manifest.json").write_text(json.dumps(manifest))
        delivery.reconcile_published(self.config, self.job, self.digest, bot=EXPECTED_BOT)
        self.assertEqual(
            json.loads((root / "manifest.json").read_text())["state"],
            "DELIVERING",
            "Snap keeps its manifest shape",
        )
        delivery.reconcile_published(
            self.config, self.job, self.digest, bot=EXPECTED_BOT, manifest_state="COMPLETED"
        )
        self.assertEqual(
            json.loads((root / "manifest.json").read_text())["state"],
            "COMPLETED",
            "guard:status-manifest-state",
        )

    def test_missing_ack_is_refused(self):
        root = self.publish()
        with self.repo.transaction() as db:
            db.execute("UPDATE telegram_outbox SET message_id=NULL")
        with self.assertRaisesRegex(ValueError, "receipt missing", msg="guard:ack-required"):
            delivery.reconcile_published(self.config, self.job, self.digest, bot=EXPECTED_BOT)

    def test_sweep_repairs_misaligned_skips_collected_and_bounds_limit(self):
        root = self.publish()
        calls = []

        def reconcile(config, job_id, video_sha):
            calls.append(job_id)
            return delivery.reconcile_published(config, job_id, video_sha, bot=EXPECTED_BOT)

        self.assertEqual(
            delivery.reconcile_evidence_sweep(self.config, bot=EXPECTED_BOT, reconcile=reconcile)[
                "repaired"
            ],
            0,
        )
        (root / "output/delivery-result.json").write_text("{}")
        result = delivery.reconcile_evidence_sweep(
            self.config, bot=EXPECTED_BOT, reconcile=reconcile
        )
        self.assertEqual((result["repaired"], calls), (1, [self.job]), "guard:sweep-repairs")
        self.assertEqual(
            json.loads((root / "output/delivery-result.json").read_text())["delivery"], "PUBLISHED"
        )
        import shutil

        shutil.rmtree(root)
        self.assertEqual(
            delivery.reconcile_evidence_sweep(self.config, bot=EXPECTED_BOT, reconcile=reconcile)[
                "skipped"
            ],
            1,
            "guard:no-recreate-collected",
        )
        self.assertFalse(root.exists())
        for limit in (0, 101, True, "5"):
            with (
                self.subTest(limit=limit),
                self.assertRaisesRegex(ValueError, "sweep limit", msg="guard:sweep-limit"),
            ):
                delivery.reconcile_evidence_sweep(
                    self.config, limit=limit, bot=EXPECTED_BOT, reconcile=reconcile
                )

    def test_sweep_counts_a_failed_repair_as_pending(self):
        root = self.publish()
        (root / "output/delivery-result.json").write_text("{}")

        def broken(*args):
            raise OSError("disk")

        self.assertEqual(
            delivery.reconcile_evidence_sweep(self.config, bot=EXPECTED_BOT, reconcile=broken)[
                "pending"
            ],
            1,
            "guard:sweep-pending",
        )

    def test_app_wrappers_bind_the_bot_and_shape(self):
        from snap_bot import telegram_egress as snap

        root = self.publish()
        (root / "output/delivery-result.json").unlink()
        self.assertEqual(
            snap.reconcile_published(self.config, self.job, self.digest)["bot"], EXPECTED_BOT
        )
        self.assertIs(snap.atomic_json, delivery.atomic_json)
        self.assertIs(snap.DeliveryConfirmed, delivery.DeliveryConfirmed)


# Run only the reconciliation tests here; the base delivery tests run in their own module.
for _name in [n for n in dir(DeliveryTests) if n.startswith("test_")]:
    if not hasattr(ReconcileFromDurableAck, _name) or getattr(
        ReconcileFromDurableAck, _name
    ) is getattr(DeliveryTests, _name):
        setattr(ReconcileFromDurableAck, _name, None)
del DeliveryTests

if __name__ == "__main__":
    unittest.main()
