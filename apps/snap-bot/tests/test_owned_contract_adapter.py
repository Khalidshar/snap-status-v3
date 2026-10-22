import gc
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from snap_bot.did_you_know import load_facts, make_content, rotation_order
from snap_bot.owned_contract_adapter import (
    ContractReconciliationRequired,
    ContractViolation,
    contract_status,
    digest,
    initialize,
    reconcile_stage,
    require_delivery_contract,
    run_stage,
)
from studio_core.config import Config
from studio_core.jobs import LeaseLost, Repository

ROOT = Path(__file__).parents[3]
PROFILE = {
    "width": 1080,
    "height": 1920,
    "duration_seconds": 8,
    "max_bytes": 50000000,
    "font_sha256": "a" * 64,
    "latin_font_sha256": "b" * 64,
}


def box(kind, payload=b""):
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def faststart_mp4(marker=b"owned synthetic video"):
    """Smallest top-level ISO BMFF layout the media agent accepts: ftyp, moov, mdat."""
    return (
        box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2avc1mp41")
        + box(b"moov", b"synthetic")
        + box(b"mdat", marker)
    )


def reserve(repo, job, content, now):
    """Record the reservation choose_fact would have made for this synthetic content."""
    with repo.transaction() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS fact_reservations(job_id TEXT PRIMARY KEY REFERENCES jobs(id),fact_id TEXT NOT NULL,body_hash TEXT NOT NULL,reserved_at REAL NOT NULL,content_json TEXT NOT NULL)"
        )
        db.execute(
            "INSERT OR IGNORE INTO fact_reservations VALUES(?,?,?,?,?)",
            (
                job,
                content["fact_id"],
                hashlib.sha256(content["body"].encode()).hexdigest(),
                now,
                json.dumps(content, ensure_ascii=False, sort_keys=True),
            ),
        )


class OwnedContractAdapterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(gc.collect)
        self.root = Path(temp.name)
        self.now = time.time()
        # Approval fixtures must share the live delivery clock, not a calendar deadline.
        self.config = replace(
            Config.load(ROOT / "config/snap/weather.toml"),
            data_root=self.root / "data",
            owners={"telegram": ["synthetic-reviewer"]},
        )
        self.repo = Repository(self.config, clock=lambda: self.now)
        job, _ = self.repo.enqueue(
            "adapter", "synthetic", "did_you_know", "NORMAL", "input", self.now + 900
        )
        self.job = job["id"]
        self.claim = self.repo.claim(job_id=self.job)
        self.token = self.claim["lease_token"]
        self.attempt = 1
        # The first fact of the first rotation category, as choose_fact would reserve it.
        self.content = make_content(
            next(f for f in load_facts() if f["category"] == rotation_order(None)[0]), self.now
        )
        self.video = faststart_mp4()
        self.result = {
            "job_id": self.job,
            "attempt": 1,
            "content_hash": self.content["content_hash"],
            "video_sha256": hashlib.sha256(self.video).hexdigest(),
            "qa": {
                "result": "PASS",
                "width": 1080,
                "height": 1920,
                "duration": 8.0,
                "bytes": len(self.video),
                "codec": "h264",
                "audio": "generated_ambient",
                "audio_qa": {"integrated_lufs": -18.0, "true_peak_dbtp": -3.0, "phone_lufs": -21.0},
                "visual_review": "PENDING",
            },
            "font_sha256": PROFILE["font_sha256"],
            "latin_font_sha256": PROFILE["latin_font_sha256"],
            "glyph_coverage": "PASS",
            "safe_area": {"result": "PASS", "boxes": []},
            "content_type": "did_you_know",
            "artifact": f"renders/{self.job}/attempts/1/output/did-you-know.mp4",
        }
        self.receipt = {
            "job_id": self.job,
            "attempt": 1,
            "stage": "design",
            "sha256": self.result["video_sha256"],
            "path": self.result["artifact"],
        }

    def stage(self, role, payload, callback, **kwargs):
        return run_stage(
            self.repo, self.job, self.token, self.attempt, role, payload, callback, **kwargs
        )

    def source(self):
        return self.stage("fact_source", {"command": "did_you_know"}, lambda: (self.content, []))

    def arabic(self):
        from snap_bot.arabic_agent import check_content

        return self.stage("arabic", self.content, lambda: check_content(self.content))

    def checks(self):
        from snap_bot.content_agents import CHECKS, run_check

        reserve(self.repo, self.job, self.content, self.now)
        for role in CHECKS:
            self.stage(
                role,
                self.content,
                lambda role=role: run_check(
                    role, self.repo, self.job, self.token, self.attempt, self.content
                ),
            )

    def rendered(self):
        self.source()
        self.arabic()
        self.checks()
        return self.stage("design", self.content, lambda: self.result)

    def render_checks(self):
        from snap_bot.render_agents import RENDER_CHECKS, run_render_check

        settings = self.artifacts()
        for role in RENDER_CHECKS:
            self.stage(
                role,
                self.result,
                lambda role=role: run_render_check(
                    role, self.repo, self.job, self.token, self.attempt, self.result, settings
                ),
            )

    def design(self):
        """Everything quality depends on: content checks, the render and the render checks."""
        result = self.rendered()
        self.render_checks()
        return result

    def adopted(self, *, final=True):
        self.design()
        for state in ("RESEARCHING", "PROCESSING", "DOWNLOADING", "DESIGNING", "RENDERING"):
            self.repo.transition(self.job, self.token, state)
        self.repo.record_stage(
            self.job, self.token, 1, "design", self.result["video_sha256"], self.result["artifact"]
        )
        self.quality_receipt()
        receipt = self.stage("quality", self.result, lambda: self.receipt)
        self.repo.transition(self.job, self.token, "VALIDATING")
        if final:
            from snap_bot.final_agent import evaluate_final, record_human_frame

            record_human_frame(
                self.repo, self.job, self.token, 1, self.result, reviewer="synthetic-reviewer"
            )
            evaluate_final(self.repo, self.job, self.token, 1, self.result)
        return receipt

    def quality_receipt(self):
        encoded = json.dumps(self.result, ensure_ascii=False, sort_keys=True, indent=2).encode()
        self.repo.record_stage(
            self.job,
            self.token,
            1,
            "quality",
            hashlib.sha256(encoded).hexdigest(),
            str(Path(self.result["artifact"]).parent / "render-result.json").replace("\\", "/"),
        )

    def status(self, role):
        return contract_status(self.repo, self.job, 1, role)

    def artifacts(self):
        settings = {
            "spool_root": str(self.root / "spool/renders"),
            "input_root": str(self.root / "inputs/renders"),
            "profile": PROFILE,
        }
        video = Path(settings["spool_root"]).parent / self.result["artifact"]
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(self.video)
        (video.parent / "render-result.json").write_bytes(
            json.dumps(self.result, ensure_ascii=False, sort_keys=True, indent=2).encode()
        )
        (video.parent / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        path = Path(settings["input_root"]) / self.job / "attempts/1/input/content.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.content, ensure_ascii=False), encoding="utf-8")
        return settings

    def test_invalid_input_commits_failure_without_callback(self):
        calls = []
        with self.assertRaises(ContractViolation):
            self.stage("fact_source", {}, lambda: calls.append(1))
        self.assertEqual(calls, [])
        self.assertEqual(self.status("fact_source"), "FAILED")
        with self.repo.connect() as db:
            with self.assertRaises(ContractViolation):
                require_delivery_contract(db, self.job, self.result)
            from snap_bot.owned_contract_adapter import quality_chain

            with self.assertRaises(ContractViolation):
                quality_chain(db, self.job, 1, self.result)

    def test_missing_contract_table_and_empty_job_fail_closed(self):
        for exists in (False, True):
            with self.subTest(table=exists), self.repo.connect() as db:
                if exists:
                    initialize(db)
                with self.assertRaisesRegex(ContractViolation, "^CONTRACT_DELIVERY_BLOCKED$"):
                    require_delivery_contract(db, self.job, self.result)

    def test_legacy_is_only_explicit_original_creation_policy(self):
        with self.repo.transaction() as db:
            self.repo.event(
                db,
                self.job,
                "legacy.preview",
                {"contract_policy": "legacy-preview-before-adapter-v1"},
            )
            with self.assertRaisesRegex(ContractViolation, "CONTRACT_DELIVERY_BLOCKED"):
                require_delivery_contract(db, self.job, self.result)
            db.execute(
                "UPDATE events SET details=? WHERE job_id=? AND event='job.queued'",
                (
                    json.dumps(
                        {
                            "command": "did_you_know",
                            "contract_policy": "legacy-preview-before-adapter-v1",
                        }
                    ),
                    self.job,
                ),
            )
            require_delivery_contract(db, self.job, self.result)
            initialize(db)
            require_delivery_contract(db, self.job, self.result)
        with self.assertRaises(ContractViolation):
            self.stage("fact_source", {}, lambda: None)
        with self.repo.connect() as db:
            with self.assertRaises(ContractViolation):
                require_delivery_contract(db, self.job, self.result)

    def test_malformed_or_wrong_creation_policy_cannot_exempt(self):
        values = [
            "{",
            json.dumps(
                {"command": "weather", "contract_policy": "legacy-preview-before-adapter-v1"}
            ),
            json.dumps({"command": "did_you_know", "contract_policy": True}),
        ]
        for details in values:
            with self.subTest(details=details), self.repo.transaction() as db:
                db.execute(
                    "UPDATE events SET details=? WHERE job_id=? AND event='job.queued'",
                    (details, self.job),
                )
                with self.assertRaisesRegex(ContractViolation, "CONTRACT_DELIVERY_BLOCKED"):
                    require_delivery_contract(db, self.job, self.result)

    def test_malformed_source_output_classified_as_output_invalid(self):
        with self.assertRaisesRegex(ContractViolation, "^CONTRACT_OUTPUT_INVALID$"):
            self.stage("fact_source", {"command": "did_you_know"}, lambda: self.content)
        with self.repo.connect() as db:
            row = db.execute(
                "SELECT status,error_code FROM owned_agent_contracts WHERE job_id=? AND role='fact_source'",
                (self.job,),
            ).fetchone()
        self.assertEqual(dict(row), {"status": "FAILED", "error_code": "CONTRACT_OUTPUT_INVALID"})

    def test_legacy_creation_clock_reads_must_precede_first_claim(self):
        with self.repo.transaction() as db:
            db.execute(
                "UPDATE events SET at=?,details=? WHERE job_id=? AND event='job.queued'",
                (
                    self.now + 0.01,
                    json.dumps(
                        {
                            "command": "did_you_know",
                            "contract_policy": "legacy-preview-before-adapter-v1",
                        }
                    ),
                    self.job,
                ),
            )
            db.execute(
                "UPDATE events SET at=? WHERE job_id=? AND event='job.claimed'",
                (self.now + 0.02, self.job),
            )
            require_delivery_contract(db, self.job, self.result)
            for at in (self.now - 1, self.now + 0.03):
                db.execute(
                    "UPDATE events SET at=? WHERE job_id=? AND event='job.queued'", (at, self.job)
                )
                with self.assertRaisesRegex(ContractViolation, "CONTRACT_DELIVERY_BLOCKED"):
                    require_delivery_contract(db, self.job, self.result)

    def test_invalid_output_blocks_next_stage_and_records_no_success(self):
        with self.assertRaises(ContractViolation):
            self.stage("fact_source", {"command": "did_you_know"}, lambda: ({}, []))
        calls = []
        with self.assertRaises(ContractViolation):
            self.stage("design", self.content, lambda: calls.append(1))
        self.assertEqual(calls, [])
        self.assertEqual(self.status("fact_source"), "FAILED")
        events = [
            json.loads(e["details"])
            for e in self.repo.timeline(self.job)
            if e["event"] == "agent.run"
        ]
        self.assertNotIn("SUCCEEDED", [e["status"] for e in events])

    def test_validation_is_locked_but_callback_is_not(self):
        from snap_bot.owned_contract_adapter import validate_payload

        validated = []

        def probe(*args):
            connection = sqlite3.connect(self.repo.path, timeout=0.001)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("BEGIN IMMEDIATE")
            finally:
                connection.close()
            validated.append(args[1])
            return validate_payload(*args)

        def callback():
            with self.repo.transaction() as db:
                db.execute("SELECT 1")
            return self.content, []

        with patch("snap_bot.owned_contract_adapter.validate_payload", side_effect=probe):
            self.stage("fact_source", {"command": "did_you_know"}, callback)
        self.assertEqual(validated, ["input", "output"])

    def test_completed_callback_is_never_replayed(self):
        calls = []

        def source():
            calls.append(1)
            return self.content, []

        self.stage("fact_source", {"command": "did_you_know"}, source)
        with self.assertRaises(ContractReconciliationRequired):
            self.stage("fact_source", {"command": "did_you_know"}, source)
        self.assertEqual(calls, [1])

    def test_stale_owner_cannot_create_ledger_or_invoke_callback(self):
        self.now += self.config.lease_seconds + 1
        new = self.repo.claim(job_id=self.job)
        calls = []
        with self.assertRaises(LeaseLost):
            self.stage("fact_source", {"command": "did_you_know"}, lambda: calls.append(1))
        self.assertEqual(calls, [])
        with self.repo.connect() as db:
            self.assertFalse(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='owned_agent_contracts'"
                ).fetchone()
            )
        self.assertEqual(new["attempt"], 2)

    def test_telemetry_start_failure_does_not_execute(self):
        calls = []
        with patch("snap_bot.owned_contract_adapter.telemetry", side_effect=OSError("synthetic")):
            with self.assertRaises(ContractReconciliationRequired):
                self.stage("fact_source", {"command": "did_you_know"}, lambda: calls.append(1))
        self.assertEqual(calls, [])
        self.assertEqual(self.status("fact_source"), "READY")

    def test_telemetry_success_failure_preserves_contract_and_no_replay(self):
        from snap_bot.owned_contract_adapter import telemetry

        calls = []

        def fail_success(*args):
            if args[6] == "SUCCEEDED":
                raise OSError("synthetic")
            return telemetry(*args)

        def source():
            calls.append(1)
            return self.content, []

        with patch("snap_bot.owned_contract_adapter.telemetry", side_effect=fail_success):
            with self.assertRaises(ContractReconciliationRequired):
                self.stage("fact_source", {"command": "did_you_know"}, source)
        self.assertEqual(self.status("fact_source"), "SUCCEEDED")
        with self.assertRaises(ContractReconciliationRequired):
            self.stage("fact_source", {"command": "did_you_know"}, source)
        self.assertEqual(calls, [1])
        proof = reconcile_stage(self.repo, self.job, self.token, 1, "fact_source", 1)
        self.assertFalse(proof["execution_retried"])
        self.assertFalse(proof["delivery_attempted"])
        runs = [
            json.loads(e["details"])
            for e in self.repo.timeline(self.job)
            if e["event"] == "agent.run"
        ]
        self.assertEqual([row["status"] for row in runs], ["RUNNING", "SUCCEEDED"])
        self.assertTrue(runs[-1]["reconciled"])

    def test_foreign_content_and_attempt_render_fail_before_adoption(self):
        self.source()
        self.arabic()
        for changes in ({"content_hash": "a" * 64}, {"attempt": 2}):
            with self.assertRaises(ContractViolation):
                self.stage("design", self.content, lambda: {**self.result, **changes})
            self.assertEqual(self.status("design"), "FAILED")
            with self.repo.transaction() as db:
                db.execute(
                    "DELETE FROM owned_agent_contracts WHERE job_id=? AND role='design'",
                    (self.job,),
                )

    def test_dependency_hash_must_match_not_only_status(self):
        self.source()
        self.arabic()
        self.checks()
        other = make_content(load_facts()[1], self.now)
        calls = []
        with self.assertRaisesRegex(ContractViolation, "CONTRACT_DEPENDENCY_INVALID"):
            self.stage("design", other, lambda: calls.append(1))
        self.assertEqual(calls, [])

    def test_quality_invalid_receipt_blocks_delivery(self):
        self.design()
        with self.assertRaises(ContractViolation):
            self.stage("quality", self.result, lambda: {**self.receipt, "sha256": "a" * 64})
        self.assertEqual(self.status("quality"), "FAILED")
        with self.repo.connect() as db:
            with self.assertRaises(ContractViolation):
                require_delivery_contract(db, self.job, self.result)

    def test_delivery_requires_adoption_and_full_digest_chain(self):
        self.adopted()
        with self.repo.connect() as db:
            require_delivery_contract(db, self.job, self.result)
        with self.repo.transaction() as db:
            db.execute(
                "UPDATE owned_agent_contracts SET input_sha=? WHERE role='design'", ("a" * 64,)
            )
        with self.repo.connect() as db:
            with self.assertRaises(ContractViolation):
                require_delivery_contract(db, self.job, self.result)
            from snap_bot.owned_contract_adapter import quality_chain

            with self.assertRaises(ContractViolation):
                quality_chain(db, self.job, 1, self.result)

    def test_verified_ack_wins_even_when_contract_is_failed(self):
        self.adopted()
        with self.repo.transaction() as db:
            db.execute("UPDATE owned_agent_contracts SET status='FAILED'")
            db.execute(
                "CREATE TABLE telegram_outbox(video_sha TEXT PRIMARY KEY,job_id TEXT,state TEXT,message_id INTEGER,updated_at REAL)"
            )
            db.execute(
                "INSERT INTO telegram_outbox VALUES(?,?,?,?,?)",
                (self.result["video_sha256"], self.job, "PUBLISHED", 42, self.now),
            )
            db.execute(
                "INSERT INTO deliveries VALUES(?,?,?)",
                (self.result["video_sha256"], self.job, "PUBLISHED"),
            )
        try:
            with self.repo.connect() as db:
                require_delivery_contract(db, self.job, self.result)
        except ContractViolation as error:
            self.fail("Confirmed ACK was blocked: " + str(error))

    def test_adoption_retry_is_explicit_same_input_only(self):
        self.design()

        class Waiting(OSError):
            pass

        def crash():
            raise Waiting("synthetic")

        with self.assertRaises(Waiting):
            self.stage("quality", self.result, crash, retryable_errors=(Waiting,))
        self.assertEqual(self.status("quality"), "WAITING_ADOPTION")
        with self.assertRaises(ContractReconciliationRequired):
            self.stage("quality", self.result, lambda: self.receipt)
        self.assertEqual(
            self.stage("quality", self.result, lambda: self.receipt, retry_adoption=True),
            self.receipt,
        )

    def test_new_owner_reconciles_committed_adoption_without_execution(self):
        self.design()
        settings = self.artifacts()
        for state in ("RESEARCHING", "PROCESSING", "DOWNLOADING", "DESIGNING", "RENDERING"):
            self.repo.transition(self.job, self.token, state)

        def adopt_then_lose():
            self.repo.record_stage(
                self.job,
                self.token,
                1,
                "design",
                self.result["video_sha256"],
                self.result["artifact"],
            )
            self.quality_receipt()
            self.now += self.config.lease_seconds + 1
            self.new = self.repo.claim(job_id=self.job)
            return self.receipt

        with self.assertRaises(LeaseLost):
            self.stage("quality", self.result, adopt_then_lose)
        self.assertEqual(self.status("quality"), "RUNNING")
        with self.assertRaises(LeaseLost):
            reconcile_stage(
                self.repo,
                self.job,
                self.token,
                1,
                "quality",
                1,
                settings=settings,
                result=self.result,
            )
        proof = reconcile_stage(
            self.repo,
            self.job,
            self.new["lease_token"],
            2,
            "quality",
            1,
            settings=settings,
            result=self.result,
        )
        self.assertEqual(proof["evidence_attempt"], 1)
        self.assertEqual(proof["owner_attempt"], 2)
        self.assertEqual(self.status("quality"), "SUCCEEDED")
        self.assertEqual(self.repo.get(self.job)["state"], "RUNNING")
        self.assertIsNone(contract_status(self.repo, self.job, 2, "quality"))
        before = self.repo.timeline(self.job)
        reconcile_stage(
            self.repo,
            self.job,
            self.new["lease_token"],
            2,
            "quality",
            1,
            settings=settings,
            result=self.result,
        )
        self.assertEqual(before, self.repo.timeline(self.job))

    def test_reconciliation_rejects_missing_adoption_or_tampered_artifact(self):
        self.design()
        settings = self.artifacts()

        def lose():
            self.now += self.config.lease_seconds + 1
            self.new = self.repo.claim(job_id=self.job)
            return self.receipt

        with self.assertRaises(LeaseLost):
            self.stage("quality", self.result, lose)
        with self.assertRaises(ContractViolation):
            reconcile_stage(
                self.repo,
                self.job,
                self.new["lease_token"],
                2,
                "quality",
                1,
                settings=settings,
                result=self.result,
            )
        video = Path(settings["spool_root"]).parent / self.result["artifact"]
        video.write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            reconcile_stage(
                self.repo,
                self.job,
                self.new["lease_token"],
                2,
                "quality",
                1,
                settings=settings,
                result=self.result,
            )
        self.assertEqual(self.status("quality"), "RUNNING")

    def test_ledger_never_stores_error_messages_or_raw_content(self):
        def crash():
            raise RuntimeError("synthetic-secret-private")

        with self.assertRaises(RuntimeError):
            self.stage("fact_source", {"command": "did_you_know"}, crash)
        with self.repo.connect() as db:
            text = json.dumps(
                [dict(row) for row in db.execute("SELECT * FROM owned_agent_contracts")]
            )
        self.assertNotIn("synthetic-secret-private", text)
        self.assertNotIn(self.content["body"], text)

    def test_reconcile_tampered_confirmed_video_refused(self):
        self.adopted()
        settings = self.artifacts()
        (Path(settings["spool_root"]).parent / self.result["artifact"]).write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            reconcile_stage(
                self.repo,
                self.job,
                self.token,
                1,
                "quality",
                1,
                settings=settings,
                result=self.result,
            )


@unittest.skipUnless(os.name == "posix", "Linux owned renderer/service integration required")
class AdapterServiceTests(unittest.TestCase):
    def setUp(self):
        from test_did_you_know_render import FactRendererTests

        self.fixture = FactRendererTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.fixture
        f = self.f
        with f.repo.transaction() as db:
            db.execute("UPDATE jobs SET command='did_you_know' WHERE id=?", (f.job,))
        self.claim = {**f.owner, "command": "did_you_know"}
        self.input = Path(f.settings["input_root"]) / f.job / "attempts/1/input/content.json"
        self.input.unlink()
        self.render_calls = 0
        self.calls = []
        self.env = patch.dict(
            os.environ, {"TG_TEST_TOKEN": "123456:" + ("x" * 35), "TG_TEST_CHAT": "42"}
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def execute(self):
        import struct

        from snap_bot.service import execute

        f = self.f
        with (
            patch(
                "snap_bot.service.NetworkClient", side_effect=AssertionError("Source used network")
            ),
            patch("snap_bot.service.UnixHTTPConnection") as connection,
        ):
            response = connection.return_value.getresponse.return_value

            def reply():
                self.render_calls += 1
                f.content = json.loads(self.input.read_bytes())
                f.digest = f.content["content_hash"]
                response.status = 200
                response.read.return_value = json.dumps(self.fixture.fake_render()).encode()
                return response

            connection.return_value.getresponse.side_effect = reply
            connection.return_value.sock.getsockopt.return_value = struct.pack(
                "iII", 99, os.getuid(), os.getgid()
            )
            return execute(
                f.config, f.settings, "synthetic-gateway-secret-at-least-32", claim=self.claim
            )

    def transport(self, token, method, body=b"", kind="application/json"):
        self.calls.append(method)
        if method == "getMe":
            return {"username": "khalid_snap_v2_test_bot"}
        if method == "getChat":
            return {"id": 42, "type": "private"}
        if method == "sendVideo":
            return {"message_id": 17, "chat": {"id": 42}}
        self.fail("Unexpected method")

    def approve(self, output):
        from snap_bot.final_agent import evaluate_final, record_human_frame

        f = self.f
        f.repo.config = replace(
            f.repo.config, owners={**f.repo.config.owners, "telegram": ["synthetic-reviewer"]}
        )
        record_human_frame(
            f.repo,
            f.job,
            f.owner["lease_token"],
            1,
            output["render"],
            reviewer="synthetic-reviewer",
        )
        evaluate_final(f.repo, f.job, f.owner["lease_token"], 1, output["render"])

    def test_execute_enforces_three_contracts_and_never_sends(self):
        output = self.execute()
        f = self.f
        self.assertEqual(output["state"], "VALIDATING")
        self.assertFalse(output["external_delivery"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.render_calls, 1)
        self.assertEqual(
            [
                contract_status(f.repo, f.job, 1, role)
                for role in ("fact_source", "arabic", "design", "quality")
            ],
            ["SUCCEEDED"] * 4,
        )

    def test_failed_source_contract_prevents_render_and_delivery(self):
        with (
            patch("snap_bot.service.choose_fact", return_value=({}, [])),
            self.assertRaises(ContractViolation),
        ):
            self.execute()
        self.assertEqual(self.render_calls, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.f.repo.get(self.f.job)["state"], "FAILED")
        self.assertEqual(contract_status(self.f.repo, self.f.job, 1, "fact_source"), "FAILED")
        states = [
            json.loads(event["details"])
            for event in self.f.repo.timeline(self.f.job)
            if event["event"] == "job.state"
        ]
        self.assertEqual(states[-1], {"state": "FAILED", "code": "AGENT_CONTRACT_FAILED"})

    def test_failed_quality_contract_stops_deliver_before_any_transport(self):
        from snap_bot.render_commit import adopt_render
        from snap_bot.telegram_egress import deliver

        def invalid_receipt(*args):
            return {**adopt_render(*args), "sha256": "0" * 64}

        with (
            patch("snap_bot.service.adopt_render", side_effect=invalid_receipt),
            self.assertRaises(ContractViolation),
        ):
            self.execute()
        result = json.loads((self.f.legacy() / "render-result.json").read_bytes())
        with self.assertRaises((ContractViolation, LeaseLost)) as caught:
            deliver(
                self.f.config,
                self.f.job,
                result["video_sha256"],
                self.transport,
                review_mode="HUMAN_FRAME",
            )
        self.assertIsInstance(caught.exception, ContractViolation)
        self.assertEqual(self.calls, [])
        self.assertEqual(contract_status(self.f.repo, self.f.job, 1, "quality"), "FAILED")

    def test_quality_telemetry_failure_reconciles_and_ack_failure_never_resends(self):
        from snap_bot.owned_contract_adapter import telemetry
        from snap_bot.service import resume_render_adoption
        from snap_bot.telegram_egress import DeliveryConfirmed, deliver

        f = self.f

        def fail_success(*args):
            if args[4] == "quality" and args[6] == "SUCCEEDED":
                raise OSError("synthetic telemetry")
            return telemetry(*args)

        with (
            patch("snap_bot.owned_contract_adapter.telemetry", side_effect=fail_success),
            self.assertRaises(ContractReconciliationRequired),
        ):
            self.execute()
        self.assertEqual(f.repo.get(f.job)["state"], "RENDERING")
        self.assertEqual(contract_status(f.repo, f.job, 1, "quality"), "SUCCEEDED")
        output = resume_render_adoption(f.config, f.settings, self.claim)
        self.approve(output)
        self.assertEqual(output["state"], "VALIDATING")
        self.assertEqual(self.render_calls, 1)
        sha = output["render"]["video_sha256"]
        with patch(
            "snap_bot.telegram_egress.Storage.manifest", side_effect=OSError("synthetic receipt")
        ):
            with self.assertRaises(DeliveryConfirmed):
                deliver(
                    f.config,
                    f.job,
                    sha,
                    self.transport,
                    lease_token=f.owner["lease_token"],
                    review_mode="HUMAN_FRAME",
                )
        with f.repo.transaction() as db:
            db.execute("UPDATE owned_agent_contracts SET status='FAILED' WHERE job_id=?", (f.job,))
        proof = deliver(f.config, f.job, sha, self.transport, review_mode="HUMAN_FRAME")
        self.assertEqual(proof["delivery"], "PUBLISHED")
        self.assertEqual(self.calls.count("sendVideo"), 1)
        self.assertEqual(self.render_calls, 1)

    def test_contract_is_rechecked_in_delivery_reservation_transaction(self):
        from snap_bot.telegram_egress import deliver

        output = self.execute()
        f = self.f
        self.approve(output)

        def change_after_preflight(token, method, *args):
            reply = self.transport(token, method, *args)
            if method == "getChat":
                with f.repo.transaction() as db:
                    db.execute(
                        "UPDATE owned_agent_contracts SET status='FAILED' WHERE job_id=? AND role='quality'",
                        (f.job,),
                    )
            return reply

        with self.assertRaises(ContractViolation):
            deliver(
                f.config,
                f.job,
                output["render"]["video_sha256"],
                change_after_preflight,
                lease_token=f.owner["lease_token"],
                review_mode="HUMAN_FRAME",
            )
        self.assertNotIn("sendVideo", self.calls)
        with f.repo.connect() as db:
            self.assertFalse(
                db.execute("SELECT 1 FROM telegram_outbox WHERE job_id=?", (f.job,)).fetchone()
            )

    def test_new_owner_reconciles_real_adoption_without_render_or_send(self):
        from snap_bot.render_commit import adopt_render

        f = self.f

        def adopt_then_lose(*args):
            receipt = adopt_render(*args)
            with f.repo.transaction() as db:
                db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, f.job))
            self.new = f.repo.claim(job_id=f.job)
            return receipt

        with (
            patch("snap_bot.service.adopt_render", side_effect=adopt_then_lose),
            self.assertRaises(LeaseLost),
        ):
            self.execute()
        result = json.loads((f.legacy() / "render-result.json").read_bytes())
        self.assertEqual(contract_status(f.repo, f.job, 1, "quality"), "RUNNING")
        proof = reconcile_stage(
            f.repo,
            f.job,
            self.new["lease_token"],
            2,
            "quality",
            1,
            settings=f.settings,
            result=result,
        )
        self.assertEqual(proof["status"], "RECONCILED")
        self.assertEqual(self.render_calls, 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(f.repo.get(f.job)["state"], "RUNNING")
        self.assertIsNone(contract_status(f.repo, f.job, 2, "quality"))
