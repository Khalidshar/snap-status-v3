import hashlib
import json
import os
import struct
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from snap_bot.telegram_egress import EXPECTED_BOT, deliver
from snap_bot.weather import content_hash
from studio_core.config import Config
from studio_core.jobs import Repository
from studio_core.storage import Storage


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"),
            data_root=Path(self.temp.name),
        )
        self.repo = Repository(self.config)
        job, _ = self.repo.enqueue("test", "owner", "weather", "NORMAL", "input", time.time() + 600)
        self.job = job["id"]
        claim = self.repo.claim()
        with self.repo.transaction() as db:
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, self.job))
        root = Storage(self.config).workspace(self.job)
        self.video = b"test reviewed artifact"
        self.digest = hashlib.sha256(self.video).hexdigest()
        content = {
            "type": "weather",
            "sacred": False,
            "expires_at": time.time() + 600,
            "temperature_c": 29.0,
        }
        content["content_hash"] = content_hash(content)
        (root / "input/content.json").write_text(json.dumps(content))
        result = {
            "video_sha256": self.digest,
            "content_hash": content["content_hash"],
            "qa": {"result": "PASS"},
            "glyph_coverage": "PASS",
            "network_isolation": {"ipv4": True},
        }
        (root / "output/render-result.json").write_text(json.dumps(result))
        (root / "output/weather.mp4").write_bytes(self.video)
        self.calls = []
        self.env = patch.dict(
            os.environ, {"TG_TEST_TOKEN": "123456:" + ("x" * 35), "TG_TEST_CHAT": "42"}
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def transport(self, token, method, body=b"", kind="application/json"):
        self.calls.append(method)
        if method == "getMe":
            return {"username": EXPECTED_BOT}
        if method == "getChat":
            return {"id": 42, "type": "private"}
        return {"message_id": 7, "chat": {"id": 42}}

    def test_confirmed_delivery_terminal_and_replay_suppressed(self):
        self.assertEqual(
            deliver(self.config, self.job, self.digest, self.transport)["delivery"], "PUBLISHED"
        )
        self.assertEqual(self.repo.get(self.job)["state"], "COMPLETED")
        deliver(self.config, self.job, self.digest, self.transport)
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_unknown_result_never_replayed(self):
        def transport(*args):
            if args[1] == "sendVideo":
                self.calls.append("sendVideo")
                raise TimeoutError()
            return self.transport(*args)

        with self.assertRaisesRegex(RuntimeError, "DELIVERY_UNKNOWN"):
            deliver(self.config, self.job, self.digest, transport)
        self.assertEqual(
            deliver(self.config, self.job, self.digest, self.transport)["delivery"],
            "DELIVERY_UNKNOWN",
        )
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_changed_video_rejected_before_network(self):
        (Storage(self.config).workspace(self.job) / "output/weather.mp4").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            deliver(self.config, self.job, self.digest, self.transport)
        self.assertEqual(self.calls, [])

    def test_new_request_for_published_video_completes_without_duplicate_send(self):
        deliver(self.config, self.job, self.digest, self.transport)
        job, _ = self.repo.enqueue(
            "second", "owner", "weather", "NORMAL", "input", time.time() + 600
        )
        claim = self.repo.claim()
        root = Storage(self.config).workspace(job["id"])
        original = Storage(self.config).workspace(self.job)
        for name in ("input/content.json", "output/render-result.json", "output/weather.mp4"):
            (root / name).write_bytes((original / name).read_bytes())
        for state in (
            "RESEARCHING",
            "PROCESSING",
            "DOWNLOADING",
            "DESIGNING",
            "RENDERING",
            "VALIDATING",
        ):
            self.repo.transition(job["id"], claim["lease_token"], state)
        result = deliver(
            self.config, job["id"], self.digest, self.transport, lease_token=claim["lease_token"]
        )
        self.assertEqual(result["delivery"], "PUBLISHED")
        self.assertEqual(self.repo.get(job["id"])["state"], "COMPLETED")
        self.assertEqual(json.loads((root / "manifest.json").read_text())["delivery"], "PUBLISHED")
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_local_finalization_failure_preserves_known_acknowledgement(self):
        with patch("snap_bot.telegram_egress.Storage.manifest", side_effect=OSError("disk")):
            with self.assertRaisesRegex(RuntimeError, "Delivery confirmed"):
                deliver(self.config, self.job, self.digest, self.transport)
        self.assertEqual(
            deliver(self.config, self.job, self.digest, self.transport)["delivery"], "PUBLISHED"
        )
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_outbox_and_delivering_transition_are_atomic(self):
        transition = Repository.transition

        def fail_transition(repository, job, token, state, details=None, *, db=None):
            if state == "DELIVERING":
                raise RuntimeError("injected transition failure")
            return transition(repository, job, token, state, details, db=db)

        with patch("snap_bot.telegram_egress.Repository.transition", new=fail_transition):
            with self.assertRaisesRegex(RuntimeError, "injected transition failure"):
                deliver(self.config, self.job, self.digest, self.transport)
        self.assertEqual(self.repo.get(self.job)["state"], "VALIDATING")
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
        self.assertEqual(self.calls.count("sendVideo"), 0)
        lease = self.repo.get(self.job)["lease_token"]
        self.assertEqual(
            deliver(self.config, self.job, self.digest, self.transport, lease_token=lease)[
                "delivery"
            ],
            "PUBLISHED",
        )
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_targeted_delivery_does_not_claim_other_job(self):
        job, _ = self.repo.enqueue(
            "older-high", "other-owner", "weather", "CRITICAL", "input", time.time() + 600
        )
        deliver(self.config, self.job, self.digest, self.transport)
        self.assertEqual(self.repo.get(job["id"])["state"], "QUEUED")
        self.assertEqual(sum(e["event"] == "job.claimed" for e in self.repo.timeline(job["id"])), 0)

    def test_confirmed_ack_reconciled_after_completion_failure(self):
        original = Repository.transition

        def fail_complete(repository, job, token, state, details=None, *, db=None):
            if state == "COMPLETED":
                raise RuntimeError("injected completion failure")
            return original(repository, job, token, state, details, db=db)

        with patch("snap_bot.telegram_egress.Repository.transition", new=fail_complete):
            self.assertEqual(
                deliver(self.config, self.job, self.digest, self.transport)["delivery"], "PUBLISHED"
            )
        self.assertEqual(self.repo.get(self.job)["state"], "COMPLETED")
        with self.repo.connect() as db:
            self.assertEqual(
                db.execute("SELECT state FROM telegram_outbox").fetchone()[0], "PUBLISHED"
            )
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, self.job))
        self.assertIsNone(self.repo.claim())
        self.assertEqual(self.repo.get(self.job)["state"], "COMPLETED")
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_changed_hash_for_same_job_never_sends_again(self):
        deliver(self.config, self.job, self.digest, self.transport)
        root = Storage(self.config).workspace(self.job)
        changed = b"changed reviewed artifact"
        digest = hashlib.sha256(changed).hexdigest()
        result = json.loads((root / "output/render-result.json").read_text())
        result["video_sha256"] = digest
        (root / "output/render-result.json").write_text(json.dumps(result))
        (root / "output/weather.mp4").write_bytes(changed)
        with self.assertRaisesRegex(RuntimeError, "DELIVERY_ARTIFACT_CHANGED"):
            deliver(self.config, self.job, digest, self.transport)
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_stale_owner_during_delivery_preparation_cannot_send(self):
        claim = self.repo.claim(job_id=self.job)
        for state in (
            "RESEARCHING",
            "PROCESSING",
            "DOWNLOADING",
            "DESIGNING",
            "RENDERING",
            "VALIDATING",
        ):
            self.repo.transition(self.job, claim["lease_token"], state)
        original = Repository.heartbeat

        def swap_owner(repository, job, token):
            original(repository, job, token)
            with repository.connect() as db:
                db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job))
            repository.claim(job_id=job)

        with patch("snap_bot.telegram_egress.Repository.heartbeat", new=swap_owner):
            from studio_core.jobs import LeaseLost

            with self.assertRaises(LeaseLost):
                deliver(
                    self.config,
                    self.job,
                    self.digest,
                    self.transport,
                    lease_token=claim["lease_token"],
                )
        self.assertEqual(self.calls.count("sendVideo"), 0)
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0], 0)

    def test_transient_heartbeat_database_lock_does_not_kill_renewal(self):
        import sqlite3

        self.config = replace(self.config, lease_seconds=1)
        original = Repository.heartbeat
        renewals = []

        def heartbeat(repository, job, token):
            renewals.append(1)
            if len(renewals) == 1:
                raise sqlite3.OperationalError("synthetic database lock")
            return original(repository, job, token)

        def transport(*args):
            if args[1] == "sendVideo":
                time.sleep(0.8)
            return self.transport(*args)

        with patch("snap_bot.telegram_egress.Repository.heartbeat", new=heartbeat):
            self.assertEqual(
                deliver(self.config, self.job, self.digest, transport)["delivery"], "PUBLISHED"
            )
        self.assertGreaterEqual(len(renewals), 2)
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_two_jobs_same_video_concurrent_never_send_twice(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        self.config = replace(self.config, max_running=2)
        repo = Repository(self.config)
        second, _ = repo.enqueue(
            "concurrent-second", "owner-two", "weather", "NORMAL", "input", time.time() + 600
        )
        original = Storage(self.config).workspace(self.job)
        other = Storage(self.config).workspace(second["id"])
        for name in ("input/content.json", "output/render-result.json", "output/weather.mp4"):
            (other / name).write_bytes((original / name).read_bytes())
        claims = [repo.claim(job_id=job) for job in (self.job, second["id"])]
        for claim in claims:
            for state in (
                "RESEARCHING",
                "PROCESSING",
                "DOWNLOADING",
                "DESIGNING",
                "RENDERING",
                "VALIDATING",
            ):
                repo.transition(claim["id"], claim["lease_token"], state)
        ready = threading.Barrier(2)

        def transport(*args):
            if args[1] == "getChat":
                ready.wait(timeout=5)
            if args[1] == "sendVideo":
                time.sleep(0.05)
            return self.transport(*args)

        def worker(claim):
            try:
                return deliver(
                    self.config,
                    claim["id"],
                    self.digest,
                    transport,
                    lease_token=claim["lease_token"],
                )
            except RuntimeError as error:
                self.assertIn("DELIVERY_ALREADY_RESERVED", str(error))
                return {"delivery": "RECONCILIATION_REQUIRED"}

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, claims))
        self.assertEqual(self.calls.count("sendVideo"), 1)
        self.assertTrue(any(result["delivery"] == "PUBLISHED" for result in results))

    def test_late_ack_after_unknown_sweep_completes_without_resend(self):
        def transport(*args):
            if args[1] == "sendVideo":
                with self.repo.connect() as db:
                    db.execute(
                        "UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, self.job)
                    )
                self.assertIsNone(self.repo.claim())
                self.assertEqual(self.repo.get(self.job)["error_code"], "DELIVERY_UNKNOWN")
            return self.transport(*args)

        self.assertEqual(
            deliver(self.config, self.job, self.digest, transport)["delivery"], "PUBLISHED"
        )
        root = Storage(self.config).workspace(self.job)
        self.assertEqual(
            json.loads((root / "output/delivery-result.json").read_text())["message_id"], 7
        )
        self.assertTrue(json.loads((root / "manifest.json").read_text())["external_delivery"])
        self.assertEqual(self.repo.get(self.job)["state"], "COMPLETED")
        self.assertIsNone(self.repo.get(self.job)["error_code"])
        self.assertIsNone(self.repo.claim())
        self.assertIsNone(self.repo.claim())
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_unclaimed_other_job_published_receipt_updates_job(self):
        deliver(self.config, self.job, self.digest, self.transport)
        job, _ = self.repo.enqueue(
            "shared-video-no-token", "owner", "weather", "NORMAL", "input", time.time() + 600
        )
        root = Storage(self.config).workspace(job["id"])
        original = Storage(self.config).workspace(self.job)
        for name in ("input/content.json", "output/render-result.json", "output/weather.mp4"):
            (root / name).write_bytes((original / name).read_bytes())
        result = deliver(self.config, job["id"], self.digest, self.transport)
        self.assertEqual(result["delivery"], "PUBLISHED")
        self.assertEqual(self.repo.get(job["id"])["state"], "COMPLETED")
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_changed_artifact_rejection_releases_owned_lease(self):
        claim = self.repo.claim(job_id=self.job)
        for state in (
            "RESEARCHING",
            "PROCESSING",
            "DOWNLOADING",
            "DESIGNING",
            "RENDERING",
            "VALIDATING",
        ):
            self.repo.transition(self.job, claim["lease_token"], state)
        with self.repo.transaction() as db:
            db.execute(
                "CREATE TABLE telegram_outbox(video_sha TEXT PRIMARY KEY,job_id TEXT NOT NULL,state TEXT NOT NULL,message_id INTEGER,updated_at REAL NOT NULL)"
            )
            db.execute(
                "INSERT INTO telegram_outbox VALUES(?,?,?,NULL,?)",
                ("different-hash", self.job, "SENDING", time.time()),
            )
        with self.assertRaisesRegex(RuntimeError, "DELIVERY_ARTIFACT_CHANGED"):
            deliver(
                self.config, self.job, self.digest, self.transport, lease_token=claim["lease_token"]
            )
        self.assertEqual(self.repo.get(self.job)["state"], "FAILED")
        self.assertIsNone(self.repo.get(self.job)["lease_token"])
        self.assertEqual(self.calls.count("sendVideo"), 0)

    def test_unknown_without_confirmed_receipt_stays_failed(self):
        claim = self.repo.claim(job_id=self.job)
        for state in (
            "RESEARCHING",
            "PROCESSING",
            "DOWNLOADING",
            "DESIGNING",
            "RENDERING",
            "VALIDATING",
            "DELIVERING",
        ):
            self.repo.transition(self.job, claim["lease_token"], state)
        with self.repo.connect() as db:
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, self.job))
        self.repo.claim()
        self.repo.claim()
        self.assertEqual(self.repo.get(self.job)["state"], "FAILED")
        self.assertEqual(self.repo.get(self.job)["error_code"], "DELIVERY_UNKNOWN")

    def test_no_lease_reconciliation_during_claim_returns_receipt(self):
        deliver(self.config, self.job, self.digest, self.transport)
        with self.repo.connect() as db:
            db.execute(
                "UPDATE jobs SET state='FAILED',error_code='DELIVERY_UNKNOWN' WHERE id=?",
                (self.job,),
            )
        result = deliver(self.config, self.job, self.digest, self.transport)
        self.assertEqual(result["delivery"], "PUBLISHED")
        self.assertEqual(self.repo.get(self.job)["state"], "COMPLETED")
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_terminal_receipt_rebuilds_missing_local_evidence(self):
        deliver(self.config, self.job, self.digest, self.transport)
        root = Storage(self.config).workspace(self.job)
        (root / "output/delivery-result.json").unlink()
        (root / "manifest.json").write_text(
            json.dumps({"job_id": self.job, "external_delivery": False})
        )
        receipt = deliver(self.config, self.job, self.digest, self.transport)
        self.assertEqual(receipt["delivery"], "PUBLISHED")
        self.assertTrue(json.loads((root / "manifest.json").read_text())["external_delivery"])
        self.assertEqual(
            json.loads((root / "output/delivery-result.json").read_text())["message_id"], 7
        )
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_worker_handles_confirmed_delivery_as_reconciliation(self):
        from snap_bot.command_worker import confirmed_failure
        from snap_bot.telegram_egress import DeliveryConfirmed

        with patch(
            "snap_bot.telegram_egress.Storage.manifest", side_effect=OSError("synthetic disk fault")
        ):
            with self.assertRaises(DeliveryConfirmed) as caught:
                deliver(self.config, self.job, self.digest, self.transport)
        with patch("snap_bot.command_worker.print") as logger:
            result = confirmed_failure(self.config, self.job, caught.exception)
        self.assertEqual(result["delivery"], "PUBLISHED")
        self.assertIn("command.completed", logger.call_args.args[0])
        self.assertNotIn("command.failed", logger.call_args.args[0])
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_periodic_sweep_repairs_transient_evidence_fault_without_network(self):
        from snap_bot.command_worker import confirmed_failure
        from snap_bot.telegram_egress import DeliveryConfirmed, reconcile_evidence_sweep

        with patch(
            "snap_bot.telegram_egress.Storage.manifest", side_effect=OSError("synthetic disk fault")
        ):
            with self.assertRaises(DeliveryConfirmed) as caught:
                deliver(
                    self.config,
                    self.job,
                    self.digest,
                    self.transport,
                    review_mode="PINNED_WEATHER_TEMPLATE",
                )
            self.assertIsNone(confirmed_failure(self.config, self.job, caught.exception))
            self.assertEqual(reconcile_evidence_sweep(self.config)["pending"], 1)
        calls = list(self.calls)
        result = reconcile_evidence_sweep(self.config)
        self.assertEqual(result["repaired"], 1)
        self.assertEqual(result["pending"], 0)
        root = Storage(self.config).workspace(self.job)
        manifest = json.loads((root / "manifest.json").read_text())
        self.assertTrue(manifest["external_delivery"])
        self.assertEqual(manifest["delivery_receipt"]["review_mode"], "PINNED_WEATHER_TEMPLATE")
        self.assertNotEqual(manifest.get("visual_review"), "PASS")
        self.assertEqual(self.calls, calls)
        self.assertEqual(self.calls.count("sendVideo"), 1)
        self.assertEqual(reconcile_evidence_sweep(self.config)["repaired"], 0)

    def test_worker_repair_preserves_pinned_review_mode(self):
        from snap_bot.command_worker import confirmed_failure
        from snap_bot.telegram_egress import DeliveryConfirmed

        with patch(
            "snap_bot.telegram_egress.Storage.manifest", side_effect=OSError("synthetic disk fault")
        ):
            with self.assertRaises(DeliveryConfirmed) as caught:
                deliver(
                    self.config,
                    self.job,
                    self.digest,
                    self.transport,
                    review_mode="PINNED_WEATHER_TEMPLATE",
                )
        self.assertEqual(caught.exception.review_mode, "PINNED_WEATHER_TEMPLATE")
        receipt = confirmed_failure(self.config, self.job, caught.exception)
        self.assertEqual(receipt["review_mode"], "PINNED_WEATHER_TEMPLATE")
        # Durable provenance survives a lost exception and a later default argument.
        from snap_bot.telegram_egress import reconcile_published

        self.assertEqual(
            reconcile_published(self.config, self.job, self.digest, "HUMAN_FRAME")["review_mode"],
            "PINNED_WEATHER_TEMPLATE",
        )

    def test_ack_survives_both_transition_and_reconciliation_failures(self):
        from snap_bot.telegram_egress import DeliveryConfirmed, reconcile_evidence_sweep

        transition = Repository.transition

        def fail_complete(repository, job, token, state, details=None, *, db=None):
            if state == "COMPLETED":
                raise RuntimeError("synthetic transition fault")
            return transition(repository, job, token, state, details, db=db)

        with (
            patch("snap_bot.telegram_egress.Repository.transition", new=fail_complete),
            patch(
                "snap_bot.telegram_egress.Repository.reconcile_delivery",
                side_effect=RuntimeError("synthetic reconciliation fault"),
            ),
        ):
            with self.assertRaises(DeliveryConfirmed):
                deliver(
                    self.config,
                    self.job,
                    self.digest,
                    self.transport,
                    review_mode="PINNED_WEATHER_TEMPLATE",
                )
        with self.repo.connect() as db:
            self.assertEqual(
                db.execute("SELECT state,message_id FROM telegram_outbox").fetchone()["state"],
                "PUBLISHED",
            )
            self.assertEqual(
                db.execute("SELECT state FROM deliveries WHERE job_id=?", (self.job,)).fetchone()[
                    "state"
                ],
                "PUBLISHED",
            )
        self.assertEqual(reconcile_evidence_sweep(self.config)["repaired"], 1)
        self.assertEqual(self.repo.get(self.job)["state"], "COMPLETED")
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_sweep_does_not_recreate_collected_workspace(self):
        import shutil

        from snap_bot.telegram_egress import reconcile_evidence_sweep

        deliver(self.config, self.job, self.digest, self.transport)
        root = Storage(self.config).workspace(self.job)
        shutil.rmtree(root)
        self.assertEqual(reconcile_evidence_sweep(self.config)["skipped"], 1)
        self.assertFalse(root.exists())
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_sweep_pages_past_persistent_failure_and_returns_to_it(self):
        from snap_bot.telegram_egress import reconcile_evidence_sweep

        deliver(self.config, self.job, self.digest, self.transport)
        job, _ = self.repo.enqueue(
            "sweep-second", "owner-two", "weather", "NORMAL", "input", time.time() + 600
        )
        other = Storage(self.config).workspace(job["id"])
        root = Storage(self.config).workspace(self.job)
        for name in ("input/content.json", "output/render-result.json", "output/weather.mp4"):
            (other / name).write_bytes((root / name).read_bytes())
        deliver(self.config, job["id"], self.digest, self.transport)
        for job_id in (self.job, job["id"]):
            root_job = Storage(self.config).workspace(job_id)
            (root_job / "output/delivery-result.json").unlink()
            manifest = json.loads((root_job / "manifest.json").read_text())
            manifest["external_delivery"] = False
            (root_job / "manifest.json").write_text(json.dumps(manifest))
        first, second = sorted((self.job, job["id"]))
        original = Storage.manifest

        def fail_first(storage, job_id, value):
            if job_id == first:
                raise OSError("persistent synthetic fault")
            return original(storage, job_id, value)

        with patch("snap_bot.telegram_egress.Storage.manifest", new=fail_first):
            result = reconcile_evidence_sweep(self.config, limit=1)
            self.assertEqual(result["pending"], 1)
            self.assertEqual(result["after"], first)
            result = reconcile_evidence_sweep(self.config, after=result["after"], limit=1)
            self.assertEqual(result["repaired"], 1)
            self.assertEqual(result["after"], second)
            result = reconcile_evidence_sweep(self.config, after=result["after"], limit=1)
            self.assertEqual(result["after"], "")
            self.assertEqual(reconcile_evidence_sweep(self.config, limit=1)["pending"], 1)
        self.assertEqual(reconcile_evidence_sweep(self.config)["repaired"], 1)
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_execute_preserves_confirmed_ack_through_double_state_failure(self):
        from snap_bot.service import execute
        from snap_bot.telegram_egress import DeliveryConfirmed, reconcile_evidence_sweep

        oldroot = Storage(self.config).workspace(self.job)
        content = json.loads((oldroot / "input/content.json").read_text())
        content["source"] = "synthetic"
        content["content_hash"] = content_hash(content)
        render_result = json.loads((oldroot / "output/render-result.json").read_text())
        render_result["content_hash"] = content["content_hash"]
        self.config = replace(self.config, data_root=self.config.data_root / "execute-fresh")
        self.repo = Repository(self.config)
        fresh, _ = self.repo.enqueue(
            "execute-ack", "owner", "weather", "NORMAL", "input", time.time() + 600
        )
        self.job = fresh["id"]
        claim = self.repo.claim(job_id=self.job)
        root = Storage(self.config).workspace(self.job)
        (root / "input/content.json").write_text(json.dumps(content))
        settings = {
            "profile": {"timeout_seconds": 1},
            "input_root": str(self.config.data_root / "inputs/renders"),
            "spool_root": str(self.config.data_root / "spool/renders"),
            "worker_uid": os.getuid(),
            "renderer_uid": 123,
            "render_socket": str(self.config.data_root / "render.sock"),
        }
        transition = Repository.transition

        def fail_complete(repository, job, token, state, details=None, *, db=None):
            if state == "COMPLETED":
                raise RuntimeError("synthetic completion fault")
            return transition(repository, job, token, state, details, db=db)

        def delivery(cfg, job_id, digest, token):
            return deliver(
                cfg,
                job_id,
                digest,
                self.transport,
                lease_token=token,
                review_mode="PINNED_WEATHER_TEMPLATE",
            )

        artifact = f"renders/{self.job}/attempts/{claim['attempt']}/output/weather.mp4"
        render_result.update(job_id=self.job, attempt=claim["attempt"], artifact=artifact)

        def owned_render():
            # Synthetic DB-free renderer writes only spool artifacts.
            path = Path(settings["spool_root"]).parent / artifact
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.video)
            (path.parent / "render-result.json").write_text(
                json.dumps(render_result, ensure_ascii=False, sort_keys=True, indent=2)
            )
            response.status = 200
            response.read.return_value = json.dumps(render_result).encode()
            return response

        with (
            patch(
                "snap_bot.service.observe", side_effect=lambda cfg, agent, job, callback: callback()
            ),
            patch("snap_bot.service.fetch_weather", return_value=(content, [])),
            patch("snap_bot.service.UnixHTTPConnection") as connection,
        ):
            response = connection.return_value.getresponse.return_value
            connection.return_value.getresponse.side_effect = owned_render
            connection.return_value.sock.getsockopt.return_value = struct.pack("iII", 99, 123, 123)
            with (
                patch("snap_bot.telegram_egress.Repository.transition", new=fail_complete),
                patch(
                    "snap_bot.telegram_egress.Repository.reconcile_delivery",
                    side_effect=RuntimeError("synthetic reconciliation fault"),
                ),
            ):
                with self.assertRaises(DeliveryConfirmed):
                    execute(
                        self.config,
                        settings,
                        "synthetic-gateway-signing-secret-00000000",
                        claim=claim,
                        delivery=delivery,
                    )
        # This assertion detects removal of the specialized execute exception handler.
        self.assertEqual(self.repo.get(self.job)["state"], "DELIVERING")
        self.assertEqual(reconcile_evidence_sweep(self.config)["repaired"], 1)
        self.assertEqual(self.repo.get(self.job)["state"], "COMPLETED")
        self.assertTrue(json.loads((root / "manifest.json").read_text())["external_delivery"])
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_confirmed_slice_view_is_reconciled_and_cannot_regress_under_stale_lease(self):
        from snap_bot.render_commit import owned_views

        claim = self.repo.claim(job_id=self.job)
        for state in (
            "RESEARCHING",
            "PROCESSING",
            "DOWNLOADING",
            "DESIGNING",
            "RENDERING",
            "VALIDATING",
        ):
            self.repo.transition(self.job, claim["lease_token"], state)
        root = Storage(self.config).workspace(self.job)
        slice_path = root / "output/slice-result.json"
        slice_path.write_text(
            json.dumps({"job_id": self.job, "state": "VALIDATING", "external_delivery": False})
        )
        deliver(
            self.config, self.job, self.digest, self.transport, lease_token=claim["lease_token"]
        )
        confirmed = slice_path.read_bytes()
        value = json.loads(confirmed)
        self.assertEqual(value["state"], "COMPLETED")
        self.assertTrue(value["external_delivery"])
        self.assertEqual(value["delivery_receipt"]["message_id"], 7)
        from studio_core.jobs import LeaseLost

        with self.assertRaises(LeaseLost):
            owned_views(
                self.config,
                self.repo,
                self.job,
                claim["lease_token"],
                claim["attempt"],
                {"delivery": "AWAITING"},
                {"state": "VALIDATING"},
                validating=False,
            )
        self.assertEqual(slice_path.read_bytes(), confirmed)
        deliver(self.config, self.job, self.digest, self.transport)
        self.assertEqual(json.loads(slice_path.read_bytes()), value)
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_legacy_failed_confirmed_job_with_null_code_is_repaired(self):
        from snap_bot.telegram_egress import reconcile_evidence_sweep

        deliver(self.config, self.job, self.digest, self.transport)
        root = Storage(self.config).workspace(self.job)
        (root / "output/delivery-result.json").unlink()
        with self.repo.connect() as db:
            db.execute("UPDATE jobs SET state='FAILED',error_code=NULL WHERE id=?", (self.job,))
        self.assertEqual(reconcile_evidence_sweep(self.config)["repaired"], 1)
        self.assertEqual(self.repo.get(self.job)["state"], "COMPLETED")
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_claim_reconciles_legacy_failed_confirmed_job_with_other_code(self):
        deliver(self.config, self.job, self.digest, self.transport)
        with self.repo.connect() as db:
            db.execute(
                "UPDATE jobs SET state='FAILED',error_code='VERTICAL_SLICE_FAILED' WHERE id=?",
                (self.job,),
            )
        self.assertIsNone(self.repo.claim())
        self.assertEqual(self.repo.get(self.job)["state"], "COMPLETED")
        self.assertIsNone(self.repo.get(self.job)["error_code"])
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_reconciliation_preserves_cancelled_and_unconfirmed_failed_jobs(self):
        from snap_bot.telegram_egress import reconcile_evidence_sweep

        deliver(self.config, self.job, self.digest, self.transport)
        with self.repo.connect() as db:
            db.execute("UPDATE jobs SET state='CANCELLED' WHERE id=?", (self.job,))
        self.assertFalse(self.repo.reconcile_delivery(self.job))
        self.assertEqual(reconcile_evidence_sweep(self.config)["repaired"], 0)
        self.assertEqual(self.repo.get(self.job)["state"], "CANCELLED")
        with self.repo.connect() as db:
            db.execute("UPDATE jobs SET state='FAILED',error_code=NULL WHERE id=?", (self.job,))
            db.execute("UPDATE deliveries SET state='DELIVERY_UNKNOWN' WHERE job_id=?", (self.job,))
        self.assertFalse(self.repo.reconcile_delivery(self.job))
        self.assertIsNone(self.repo.claim())
        self.assertEqual(self.repo.get(self.job)["state"], "FAILED")

    def test_legacy_provenance_wins_over_default_without_new_metadata(self):
        deliver(
            self.config,
            self.job,
            self.digest,
            self.transport,
            review_mode="PINNED_WEATHER_TEMPLATE",
        )
        with self.repo.connect() as db:
            db.execute("DELETE FROM telegram_receipt_metadata WHERE job_id=?", (self.job,))
        proof = deliver(self.config, self.job, self.digest, self.transport)
        self.assertEqual(proof["review_mode"], "PINNED_WEATHER_TEMPLATE")
        with self.repo.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM telegram_receipt_metadata").fetchone()[0], 0
            )
        from studio_core.jobs import LeaseLost

        with self.assertRaises(LeaseLost):
            deliver(
                self.config,
                self.job,
                self.digest,
                self.transport,
                lease_token="stale",
                review_mode="HUMAN_FRAME",
            )
        with self.repo.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM telegram_receipt_metadata").fetchone()[0], 0
            )
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_sweep_failure_does_not_block_readiness_or_health(self):
        from unittest.mock import Mock

        from snap_bot.command_worker import run, safe_evidence_sweep

        cfg = replace(self.config, owners={**self.config.owners, "telegram": ("42",)})
        client = Mock()
        polls = []

        def call(route, body):
            if route == "/poll":
                polls.append(1)
                if len(polls) > 1:
                    raise KeyboardInterrupt()
                return {"offset": 0, "commands": []}
            return {}

        client.call.side_effect = call
        with (
            patch.dict(
                os.environ,
                {
                    "STUDIO_GATEWAY_SECRET": "synthetic-gateway-signing-secret-00000000",
                    "TG_TEST_TOKEN": "",
                },
            ),
            patch("snap_bot.command_worker.baseline", return_value="approved"),
            patch("snap_bot.command_worker.network_guard", return_value={"ipv4": "DENIED"}),
            patch(
                "snap_bot.command_worker.reconcile_evidence_sweep",
                side_effect=OSError("synthetic maintenance fault"),
            ),
            patch("snap_bot.command_worker.Repository.claim", return_value=None),
            patch("snap_bot.command_worker.print") as logger,
        ):
            self.assertEqual(safe_evidence_sweep(cfg, after="cursor")["after"], "cursor")
            with self.assertRaises(KeyboardInterrupt):
                run(cfg, {}, "approved", client)
        health = json.loads((cfg.data_root / "listener-health.json").read_text())
        self.assertEqual(health["offset"], 0)
        self.assertEqual(health["baseline"], "approved")
        logged = " ".join(str(item.args[0]) for item in logger.call_args_list)
        self.assertIn("delivery.evidence.sweep.failed", logged)
        self.assertIn("telegram.worker.ready", logged)
        client.call.assert_any_call("/reply", {"key": "listener-v1-ready", "message": "ready"})

    def test_legacy_receipt_without_mode_does_not_invent_human_review(self):
        deliver(
            self.config,
            self.job,
            self.digest,
            self.transport,
            review_mode="PINNED_WEATHER_TEMPLATE",
        )
        with self.repo.connect() as db:
            db.execute("DELETE FROM telegram_receipt_metadata WHERE job_id=?", (self.job,))
        root = Storage(self.config).workspace(self.job)
        manifest = json.loads((root / "manifest.json").read_text())
        manifest["delivery_receipt"].pop("review_mode")
        Storage(self.config).manifest(self.job, manifest)
        receipt = deliver(self.config, self.job, self.digest, self.transport)
        self.assertEqual(receipt["review_mode"], "NOT_RECORDED")
        self.assertEqual(
            json.loads((root / "output/delivery-result.json").read_text())["review_mode"],
            "NOT_RECORDED",
        )
        self.assertEqual(
            json.loads((root / "manifest.json").read_text())["delivery_receipt"]["review_mode"],
            "NOT_RECORDED",
        )
        with self.repo.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM telegram_receipt_metadata").fetchone()[0], 0
            )
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_unspecified_new_delivery_mode_is_explicitly_not_recorded(self):
        receipt = deliver(self.config, self.job, self.digest, self.transport)
        self.assertEqual(receipt["review_mode"], "NOT_RECORDED")
        with self.repo.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT review_mode FROM telegram_receipt_metadata WHERE job_id=?", (self.job,)
                ).fetchone()[0],
                "NOT_RECORDED",
            )
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_legacy_manifest_mode_precedes_explicit_conflicting_caller_mode(self):
        deliver(self.config, self.job, self.digest, self.transport, review_mode="HUMAN_FRAME")
        with self.repo.connect() as db:
            db.execute("DELETE FROM telegram_receipt_metadata WHERE job_id=?", (self.job,))
        root = Storage(self.config).workspace(self.job)
        receipt = deliver(
            self.config,
            self.job,
            self.digest,
            self.transport,
            review_mode="PINNED_WEATHER_TEMPLATE",
        )
        self.assertEqual(receipt["review_mode"], "HUMAN_FRAME")
        self.assertEqual(
            json.loads((root / "output/delivery-result.json").read_text())["review_mode"],
            "HUMAN_FRAME",
        )
        self.assertEqual(
            json.loads((root / "manifest.json").read_text())["delivery_receipt"]["review_mode"],
            "HUMAN_FRAME",
        )
        with self.repo.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM telegram_receipt_metadata").fetchone()[0], 0
            )
        self.assertEqual(self.calls.count("sendVideo"), 1)

    def test_wrong_bot_rejected(self):
        with self.assertRaisesRegex(ValueError, "Wrong test bot"):
            deliver(self.config, self.job, self.digest, lambda *a: {"username": "other"})
        self.assertEqual(self.repo.get(self.job)["state"], "RUNNING")

    def test_wrong_owner_rejected(self):
        def transport(token, method, *args):
            return (
                {"username": EXPECTED_BOT} if method == "getMe" else {"id": 99, "type": "private"}
            )

        with self.assertRaisesRegex(ValueError, "owner"):
            deliver(self.config, self.job, self.digest, transport)


if __name__ == "__main__":
    unittest.main()
