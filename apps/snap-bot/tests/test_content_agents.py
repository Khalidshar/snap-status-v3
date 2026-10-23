import unittest

import test_owned_contract_adapter as fixtures

from snap_bot.content_agents import (
    CHECKS,
    AgentRejected,
    check_memory,
    check_rotation,
    proofread,
    run_check,
    translation_requirement,
)
from snap_bot.did_you_know import load_facts, make_content, rotation_order
from snap_bot.owned_contract_adapter import ContractViolation, digest, require_delivery_contract


def first_fact(category):
    return next(f for f in load_facts() if f["category"] == category)


class ContentAgentRuleTests(unittest.TestCase):
    def test_every_catalog_fact_passes_proofreading_and_translation(self):
        for fact in load_facts():
            with self.subTest(fact=fact["id"]):
                self.assertEqual(proofread(fact["text"])["decision"], "PASS")
                self.assertFalse(translation_requirement(fact["text"])["required"])

    def test_proofreading_rejects_each_problem_without_editing(self):
        for text, code in (
            ("قلب  الإنسان", "DOUBLE_SPACE"),
            ("قلب الإنسان ،", "SPACE_BEFORE_PUNCTUATION"),
            ("هل تعلم؟؟", "REPEATED_PUNCTUATION"),
            ("قلـب", "TATWEEL"),
            ("«قلب الإنسان", "UNBALANCED_QUOTES"),
        ):
            with (
                self.subTest(code=code),
                self.assertRaisesRegex(AgentRejected, "PROOFREADING_" + code),
            ):
                proofread(text)

    def test_translation_allows_only_approved_abbreviations(self):
        self.assertEqual(
            translation_requirement("يعمل نظام GPS بالأقمار")["approved_terms"], ["GPS"]
        )
        with self.assertRaisesRegex(AgentRejected, "TRANSLATION_REQUIRED"):
            translation_requirement("هذا fact غير مترجم")


class OwnedContentAgentTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.OwnedContractAdapterTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def other_job(self, content):
        f = self.f
        job, _ = f.repo.enqueue(
            "other-" + content["fact_id"],
            "other-actor-" + content["fact_id"],
            "did_you_know",
            "NORMAL",
            "input",
            f.now + 900,
        )
        fixtures.reserve(f.repo, job["id"], content, f.now - 10)
        return job["id"]

    def check(self, role, content=None):
        f = self.f
        with f.repo.connect() as db:
            return (check_memory if role == "memory" else check_rotation)(
                db, f.job, content or f.content
            )

    def test_memory_rejects_a_fact_still_protected_for_another_job(self):
        f = self.f
        self.other_job(f.content)
        fixtures.reserve(f.repo, f.job, f.content, f.now)
        with self.assertRaisesRegex(AgentRejected, "MEMORY_REPEAT", msg="guard:memory-repeat"):
            self.check("memory")

    def test_memory_rejects_content_that_is_not_the_reservation(self):
        f = self.f
        fixtures.reserve(f.repo, f.job, f.content, f.now)
        changed = dict(f.content, body=f.content["body"] + " تعديل")
        with self.assertRaisesRegex(
            AgentRejected, "MEMORY_CONTENT_NOT_RESERVED", msg="guard:memory-reservation"
        ):
            self.check("memory", changed)

    def test_rotation_requires_the_next_category(self):
        f = self.f
        self.other_job(make_content(first_fact("animals"), f.now - 10))
        skipped = make_content(first_fact("space"), f.now)
        fixtures.reserve(f.repo, f.job, skipped, f.now)
        with self.assertRaisesRegex(
            AgentRejected, "ROTATION_SKIPPED_CATEGORY", msg="guard:rotation-order"
        ):
            self.check("rotation", skipped)

    def test_rotation_accepts_the_next_category(self):
        f = self.f
        self.other_job(make_content(first_fact("animals"), f.now - 10))
        nxt = make_content(first_fact(rotation_order("animals")[0]), f.now)
        fixtures.reserve(f.repo, f.job, nxt, f.now)
        self.assertEqual(self.check("rotation", nxt)["previous"], "animals")

    def test_rejected_check_fails_durably_and_design_never_runs(self):
        f = self.f
        f.source()
        f.arabic()
        fixtures.reserve(f.repo, f.job, f.content, f.now)
        self.other_job(f.content)
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_CHECK_REJECTED", msg="guard:check-rejected-code"
        ):
            f.stage(
                "memory",
                f.content,
                lambda: run_check("memory", f.repo, f.job, f.token, 1, f.content),
            )
        self.assertEqual(f.status("memory"), "FAILED")
        with self.assertRaisesRegex(ContractViolation, "CONTRACT_DEPENDENCY_INVALID"):
            f.stage("rotation", f.content, lambda: f.content)
        with self.assertRaisesRegex(ContractViolation, "CONTRACT_DEPENDENCY_INVALID"):
            f.stage("design", f.content, lambda: f.result)

    def test_check_agent_cannot_rewrite_content(self):
        f = self.f
        f.source()
        f.arabic()
        # Another fully valid catalog fact: only the pass-through rule can refuse it.
        changed = make_content(
            next(x for x in load_facts() if x["id"] != f.content["fact_id"]), f.now
        )
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_OUTPUT_INVALID", msg="guard:check-pass-through"
        ):
            f.stage("memory", f.content, lambda: changed)

    def test_design_requires_every_check_in_order(self):
        f = self.f
        f.source()
        f.arabic()
        fixtures.reserve(f.repo, f.job, f.content, f.now)
        for role in CHECKS[:-1]:
            f.stage(
                role,
                f.content,
                lambda role=role: run_check(role, f.repo, f.job, f.token, 1, f.content),
            )
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_DEPENDENCY_INVALID", msg="guard:design-after-translation"
        ):
            f.stage("design", f.content, lambda: f.result)

    def test_delivery_gate_rechecks_every_check_row(self):
        for column, value, code in (
            ("status", "FAILED", "CONTRACT_DELIVERY_BLOCKED"),
            ("output_sha", "0" * 64, "CONTRACT_CHECK_CHAIN_INVALID"),
            ("input_sha", "1" * 64, "CONTRACT_CHECK_CHAIN_INVALID"),
        ):
            with self.subTest(column=column):
                f = fixtures.OwnedContractAdapterTests()
                f.setUp()
                self.addCleanup(f.doCleanups)
                f.adopted()
                with f.repo.connect() as db:
                    require_delivery_contract(db, f.job, f.result)
                with f.repo.transaction() as db:
                    db.execute(
                        "UPDATE owned_agent_contracts SET "
                        + column
                        + "=? WHERE job_id=? AND role='proofreading'",
                        (value, f.job),
                    )
                with (
                    f.repo.connect() as db,
                    self.assertRaisesRegex(
                        ContractViolation, code, msg="guard:delivery-check-chain"
                    ),
                ):
                    require_delivery_contract(db, f.job, f.result)

    def test_every_check_passes_the_reserved_content_through_unchanged(self):
        f = self.f
        f.adopted()
        with f.repo.connect() as db:
            rows = {
                r["role"]: dict(r)
                for r in db.execute("SELECT * FROM owned_agent_contracts WHERE job_id=?", (f.job,))
            }
        for role in CHECKS:
            self.assertEqual(
                (rows[role]["status"], rows[role]["input_sha"], rows[role]["output_sha"]),
                ("SUCCEEDED", digest(f.content), digest(f.content)),
            )


if __name__ == "__main__":
    unittest.main()


class CheckReconciliationTests(unittest.TestCase):
    def test_new_owner_refuses_reconciliation_when_a_check_row_was_tampered(self):
        from snap_bot.owned_contract_adapter import reconcile_stage
        from studio_core.jobs import LeaseLost

        f = fixtures.OwnedContractAdapterTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.design()
        settings = f.artifacts()
        for state in ("RESEARCHING", "PROCESSING", "DOWNLOADING", "DESIGNING", "RENDERING"):
            f.repo.transition(f.job, f.token, state)

        def adopt_then_lose():
            f.repo.record_stage(
                f.job, f.token, 1, "design", f.result["video_sha256"], f.result["artifact"]
            )
            f.quality_receipt()
            f.now += f.config.lease_seconds + 1
            f.new = f.repo.claim(job_id=f.job)
            return f.receipt

        with self.assertRaises(LeaseLost):
            f.stage("quality", f.result, adopt_then_lose)
        with f.repo.transaction() as db:
            db.execute(
                "UPDATE owned_agent_contracts SET output_sha=? WHERE job_id=? AND role='rotation'",
                ("2" * 64, f.job),
            )
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_CHECK_CHAIN_INVALID", msg="guard:reconcile-check-chain"
        ):
            reconcile_stage(
                f.repo,
                f.job,
                f.new["lease_token"],
                2,
                "quality",
                1,
                settings=settings,
                result=f.result,
            )
        self.assertEqual(f.status("quality"), "RUNNING")
