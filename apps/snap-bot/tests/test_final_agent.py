import json
import os
import unittest
from unittest.mock import patch

import test_owned_contract_adapter as fixtures

from snap_bot.agent_contract_map import validate_payload
from snap_bot.final_agent import evaluate_final, record_human_frame, require_final
from snap_bot.owned_contract_adapter import (
    ContractReconciliationRequired,
    ContractViolation,
    require_delivery_contract,
)
from studio_core.jobs import LeaseLost


class FinalAgentTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.OwnedContractAdapterTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.adopted(final=False)

    def evaluate(self):
        f = self.f
        return evaluate_final(f.repo, f.job, f.token, 1, f.result)

    def approve(self):
        f = self.f
        return record_human_frame(
            f.repo, f.job, f.token, 1, f.result, reviewer="synthetic-reviewer"
        )

    def row(self):
        with self.f.repo.connect() as db:
            return dict(
                db.execute("SELECT * FROM owned_agent_contracts WHERE role='final'").fetchone()
            )

    def gate(self, now=None):
        f = self.f
        with f.repo.connect() as db:
            require_final(db, f.job, 1, f.result, now=f.now if now is None else now)

    def test_missing_approval_is_durable_blocked(self):
        decision = self.evaluate()
        self.assertEqual(decision["delivery_decision"], "BLOCKED", msg="guard:final-missing-review")
        self.assertEqual(json.loads(self.row()["output_json"])["decision"], decision)
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_FINAL_BLOCKED", msg="guard:final-blocked-gate"
        ):
            self.gate()
        with self.f.repo.connect() as db:
            self.assertFalse(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='human_frame_reviews'"
                ).fetchone()
            )

    def test_explicit_review_and_same_evidence_approved(self):
        review = self.approve()
        decision = self.evaluate()
        self.assertEqual(review["mode"], "HUMAN_FRAME")
        self.assertEqual(review["reviewer"], "synthetic-reviewer")
        self.assertEqual(decision["delivery_decision"], "APPROVED")
        self.assertEqual(decision["review_id"], review["id"])
        self.gate()

    def test_foreign_reviewer_refused(self):
        f = self.f
        with self.assertRaisesRegex(
            ContractViolation, "FINAL_REVIEWER_UNAUTHORIZED", msg="guard:final-reviewer"
        ):
            record_human_frame(f.repo, f.job, f.token, 1, f.result, reviewer="not-an-owner")

    def test_review_binding_mismatch_blocks(self):
        for key, value in (
            ("video_sha", "a" * 64),
            ("content_hash", "b" * 64),
            ("attempt", 2),
            ("quality_sha", "c" * 64),
            ("mode", "PINNED_WEATHER_TEMPLATE"),
            ("reviewed_at", self.f.now - 86401),
        ):
            with self.subTest(key=key):
                self.approve()
                with self.f.repo.transaction() as db:
                    db.execute("UPDATE human_frame_reviews SET " + key + "=?", (value,))
                self.assertEqual(
                    self.evaluate()["delivery_decision"],
                    "BLOCKED",
                    msg="guard:final-review-binding",
                )

    def test_delivery_rechecks_final_proof(self):
        f = self.f
        self.approve()
        self.evaluate()
        original = self.row()["output_json"]
        bad = json.loads(original)
        bad["decision"]["video_sha"] = "a" * 64
        for changed in (None, json.dumps(bad)):
            with self.subTest(changed=changed):
                with f.repo.transaction() as db:
                    db.execute(
                        "UPDATE owned_agent_contracts SET output_json=? WHERE role='final'",
                        (changed,),
                    )
                with (
                    f.repo.connect() as db,
                    self.assertRaisesRegex(
                        ContractViolation, "CONTRACT_FINAL_BLOCKED", msg="guard:final-proof"
                    ),
                ):
                    require_delivery_contract(db, f.job, f.result)
        with f.repo.transaction() as db:
            db.execute(
                "UPDATE owned_agent_contracts SET output_json=? WHERE role='final'", (original,)
            )
        self.gate()

    def test_missing_final_cannot_pass_delivery(self):
        f = self.f
        with (
            f.repo.connect() as db,
            self.assertRaisesRegex(
                ContractViolation, "CONTRACT_FINAL_BLOCKED", msg="guard:final-delivery-binding"
            ),
        ):
            require_delivery_contract(db, f.job, f.result)

    def test_review_expires_at_delivery_even_after_approval(self):
        self.approve()
        self.evaluate()
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_FINAL_BLOCKED", msg="guard:final-freshness"
        ):
            self.gate(self.f.now + 86401)

    def test_recent_approval_passes_full_delivery_gate_with_relative_clock(self):
        f = self.f
        review = self.approve()
        decision = self.evaluate()
        self.assertEqual(review["reviewed_at"], f.now)
        self.assertEqual(decision["delivery_decision"], "APPROVED")
        # Exercise the default wall-clock path through the full contract chain.
        for age in (0, 60, 86400):
            with (
                self.subTest(age=age),
                patch("snap_bot.final_agent.time.time", return_value=review["reviewed_at"] + age),
            ):
                with f.repo.connect() as db:
                    require_delivery_contract(db, f.job, f.result)

    def test_86401_second_approval_blocks_full_delivery_gate(self):
        f = self.f
        review = self.approve()
        decision = self.evaluate()
        self.assertEqual(decision["delivery_decision"], "APPROVED")
        with patch("snap_bot.final_agent.time.time", return_value=review["reviewed_at"] + 86401):
            with (
                f.repo.connect() as db,
                self.assertRaisesRegex(
                    ContractViolation, "^CONTRACT_FINAL_BLOCKED$", msg="guard:final-relative-expiry"
                ),
            ):
                require_delivery_contract(db, f.job, f.result)

    def test_quality_chain_changed_blocks_and_records_failure(self):
        self.approve()
        with self.f.repo.transaction() as db:
            db.execute(
                "UPDATE owned_agent_contracts SET output_sha=? WHERE role='quality'", ("a" * 64,)
            )
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_FINAL_INPUT_INVALID", msg="guard:final-quality-chain"
        ):
            self.evaluate()
        self.assertEqual(self.row()["status"], "FAILED")
        self.assertEqual(
            json.loads(self.row()["output_json"])["decision"]["delivery_decision"], "BLOCKED"
        )

    def test_no_review_after_state_changes(self):
        f = self.f
        with f.repo.transaction() as db:
            db.execute("UPDATE jobs SET state='RENDERING' WHERE id=?", (f.job,))
        with self.assertRaisesRegex(
            ContractViolation, "FINAL_REVIEW_STATE_INVALID", msg="guard:final-review-state"
        ):
            self.approve()

    def test_stale_owner_cannot_record_or_decide(self):
        f = self.f
        f.now += f.config.lease_seconds + 1
        new = f.repo.claim(job_id=f.job)
        with self.assertRaises(LeaseLost, msg="guard:final-owner"):
            self.approve()
        with self.assertRaises(LeaseLost, msg="guard:final-owner"):
            self.evaluate()
        self.assertEqual(new["attempt"], 2)
        with f.repo.connect() as db:
            self.assertIsNone(
                db.execute("SELECT 1 FROM owned_agent_contracts WHERE role='final'").fetchone()
            )

    def test_old_attempt_review_cannot_authorize_new_owner(self):
        f = self.f
        self.approve()
        self.evaluate()
        f.now += f.config.lease_seconds + 1
        f.repo.claim(job_id=f.job)
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_FINAL_BLOCKED", msg="guard:final-current-attempt"
        ):
            self.gate()

    def test_result_committed_before_telemetry_and_no_send(self):
        self.approve()
        seen = []

        def fail(*args):
            seen.append(json.loads(self.row()["output_json"])["decision"]["delivery_decision"])
            raise OSError("synthetic telemetry")

        with (
            patch("snap_bot.final_agent.telemetry", side_effect=fail),
            self.assertRaises(ContractReconciliationRequired),
        ):
            self.evaluate()
        self.assertEqual(self.row()["status"], "SUCCEEDED", msg="guard:final-durable")
        self.assertEqual(seen, ["APPROVED"], msg="guard:final-durable")
        self.gate()
        # Explicit re-evaluation is pure/local, never render or transport.
        self.assertEqual(self.evaluate()["delivery_decision"], "APPROVED")

    def test_final_payload_contracts_reject_partial_data(self):
        for direction in ("input", "output"):
            with self.assertRaises(ValueError, msg="guard:final-payload"):
                validate_payload("final", direction, {"job_id": self.f.job, "attempt": 1})


@unittest.skipUnless(os.name == "posix", "Linux service integration")
class FinalServiceTests(unittest.TestCase):
    def test_preview_never_creates_approval_or_sends(self):
        from snap_bot.telegram_egress import deliver

        a = fixtures.AdapterServiceTests()
        a.setUp()
        self.addCleanup(a.doCleanups)
        output = a.execute()
        f = a.f
        self.assertEqual(
            output["final_decision"]["delivery_decision"], "BLOCKED", msg="guard:final-service"
        )
        with self.assertRaises(ContractViolation):
            deliver(
                f.config,
                f.job,
                output["render"]["video_sha256"],
                a.transport,
                lease_token=f.owner["lease_token"],
                review_mode="HUMAN_FRAME",
            )
        self.assertEqual(a.calls, [], msg="guard:final-no-transport")
