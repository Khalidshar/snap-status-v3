import json
import os
import unittest
from unittest.mock import patch

from snap_bot.arabic_agent import CHECKS, check_content, check_text
from snap_bot.did_you_know import load_facts, make_content
from snap_bot.owned_contract_adapter import (
    ContractReconciliationRequired,
    ContractViolation,
    contract_status,
    reconcile_stage,
    require_delivery_contract,
    telemetry,
)
from studio_core.jobs import LeaseLost


class ArabicRuleTests(unittest.TestCase):
    def test_all_reviewed_facts_pass_without_rewrite(self):
        for fact in load_facts():
            with self.subTest(fact=fact["id"]):
                content = make_content(fact, 1791000000)
                before = json.dumps(content, sort_keys=True)
                self.assertEqual(check_content(content), content)
                self.assertEqual(json.dumps(content, sort_keys=True), before)
                self.assertEqual(check_text(fact["text"])["checks"], list(CHECKS))

    def test_unknown_latin_rejected(self):
        with self.assertRaisesRegex(
            ValueError, "ARABIC_LATIN_UNAPPROVED", msg="guard:arabic-latin"
        ):
            check_text("هذه معلومة Hacker جديدة.")

    def test_ascii_punctuation_rejected_except_grouped_numbers(self):
        for text in (
            "هذه معلومة, جديدة.",
            "هل هذه معلومة?",
            "هذه معلومة; جديدة.",
            "هذه قيمة 10,20.",
        ):
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(
                    ValueError, "ARABIC_PUNCTUATION_INVALID", msg="guard:arabic-punctuation"
                ),
            ):
                check_text(text)
        self.assertEqual(check_text("القيمة 9,192,631,770 صحيحة.")["decision"], "PASS")

    def test_foreign_controls_presentation_forms_and_tatweel_rejected(self):
        for bad in ("\u202e", "\u200d", "\ufefb", "\u0640", "Т"):  # noqa: RUF001  # Cyrillic look-alike on purpose
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(
                    ValueError, "ARABIC_CHARACTER_INVALID", msg="guard:arabic-character"
                ),
            ):
                check_text("هذه " + bad + " معلومة.")

    def test_arabic_letters_required(self):
        with self.assertRaisesRegex(
            ValueError, "ARABIC_LETTERS_MISSING", msg="guard:arabic-letters"
        ):
            check_text("GPS 123.")

    def test_orphan_mark_rejected(self):
        with self.assertRaisesRegex(ValueError, "ARABIC_MARK_ORPHAN", msg="guard:arabic-mark"):
            check_text("\u064eمعلومة صحيحة.")

    def test_line_limit_rejects_long_unbreakable_word(self):
        with self.assertRaisesRegex(ValueError, "ARABIC_LINE_LIMIT", msg="guard:arabic-lines"):
            check_text("ع" * 27)

    def test_adjacent_duplicates_normalize_vowel_marks(self):
        for text in ("معلومة معلومة صحيحة.", "عِلْم علم نافع."):
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(
                    ValueError, "ARABIC_DUPLICATE_WORD", msg="guard:arabic-duplicate"
                ),
            ):
                check_text(text)

    def test_known_abbreviations_and_arabic_marks_pass(self):
        for text in (
            "عنوان IPv6 أوسع من IPv4.",
            "تقنية E Ink مفيدة.",
            "نظام GPS وتطبيق QR.",
            "تقنية NFC ومركز CERN.",
            "هذه معلومات يومًا بعد يوم.",
        ):
            with self.subTest(text=text):
                self.assertEqual(check_text(text)["decision"], "PASS")


class OwnedArabicTests(unittest.TestCase):
    def setUp(self):
        from test_owned_contract_adapter import OwnedContractAdapterTests

        self.f = OwnedContractAdapterTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.source()

    def language(self, callback=None):
        f = self.f
        return f.stage("arabic", f.content, callback or (lambda: check_content(f.content)))

    def row(self):
        with self.f.repo.connect() as db:
            return dict(
                db.execute(
                    "SELECT * FROM owned_agent_contracts WHERE job_id=? AND role='arabic'",
                    (self.f.job,),
                ).fetchone()
            )

    def test_saved_result_precedes_success_telemetry(self):
        f = self.f

        def observed(*args):
            if args[4] == "arabic" and args[6] == "SUCCEEDED":
                row = self.row()
                self.assertEqual(row["status"], "SUCCEEDED", msg="guard:arabic-durable")
                self.assertIsNotNone(row["output_json"], msg="guard:arabic-persisted")
                saved = json.loads(row["output_json"])
                self.assertEqual(saved["report"]["decision"], "PASS")
                self.assertEqual(saved["content_hash"], f.content["content_hash"])
            return telemetry(*args)

        with patch("snap_bot.owned_contract_adapter.telemetry", side_effect=observed):
            self.language()
        self.assertEqual(self.row()["input_sha"], self.row()["output_sha"])

    def test_design_without_arabic_is_blocked(self):
        f = self.f
        calls = []
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_DEPENDENCY_INVALID", msg="guard:arabic-dependency"
        ):
            f.stage("design", f.content, lambda: calls.append(1))
        # The first stage after Arabic is the memory agent; it too needs Arabic first.
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_DEPENDENCY_INVALID", msg="guard:arabic-dependency"
        ):
            f.stage("memory", f.content, lambda: calls.append(1))
        self.assertEqual(calls, [])

    def test_arabic_output_cannot_rewrite_even_to_another_valid_fact(self):
        other = make_content(load_facts()[1], self.f.now)
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_OUTPUT_INVALID", msg="guard:arabic-unchanged"
        ):
            self.language(lambda: other)
        self.assertEqual(self.row()["status"], "FAILED")

    def test_rule_failure_blocks_downstream_and_has_clear_code(self):
        f = self.f
        with self.assertRaisesRegex(ContractViolation, "CONTRACT_ARABIC_REJECTED"):
            self.language(lambda: check_content({**f.content, "body": "هذه Hacker."}))
        self.assertEqual(
            (self.row()["status"], self.row()["error_code"]), ("FAILED", "CONTRACT_ARABIC_REJECTED")
        )
        calls = []
        with self.assertRaisesRegex(ContractViolation, "CONTRACT_DEPENDENCY_INVALID"):
            f.stage("design", f.content, lambda: calls.append(1))
        self.assertEqual(calls, [])

    def test_lost_owner_has_no_language_result_or_callback(self):
        f = self.f
        f.now += f.config.lease_seconds + 1
        f.repo.claim(job_id=f.job)
        calls = []
        with self.assertRaises(LeaseLost):
            self.language(lambda: calls.append(1))
        self.assertEqual(calls, [])
        self.assertIsNone(contract_status(f.repo, f.job, 1, "arabic"))

    def test_telemetry_failure_preserves_result_and_reconciles_without_replay(self):
        f = self.f
        calls = []

        def fail(*args):
            if args[4] == "arabic" and args[6] == "SUCCEEDED":
                raise OSError("synthetic observer fault")
            return telemetry(*args)

        def check():
            calls.append(1)
            return check_content(f.content)

        with (
            patch("snap_bot.owned_contract_adapter.telemetry", side_effect=fail),
            self.assertRaises(ContractReconciliationRequired),
        ):
            self.language(check)
        self.assertEqual(self.row()["status"], "SUCCEEDED", msg="guard:arabic-durable")
        self.assertIsNotNone(self.row()["output_json"], msg="guard:arabic-persisted")
        with self.assertRaises(ContractReconciliationRequired):
            self.language(check)
        proof = reconcile_stage(f.repo, f.job, f.token, 1, "arabic", 1)
        self.assertFalse(proof["execution_retried"])
        self.assertFalse(proof["delivery_attempted"])
        self.assertEqual(calls, [1])

    def test_missing_or_tampered_arabic_result_blocks_delivery(self):
        f = self.f
        # The shared fixture executes all four real adapters and commits receipts.
        with f.repo.transaction() as db:
            db.execute("DELETE FROM owned_agent_contracts WHERE job_id=?", (f.job,))
        f.adopted()
        original = self.row()["output_json"]
        bad = json.loads(original)
        bad["report"]["decision"] = "BYPASSED"
        for changed in (None, json.dumps(bad)):
            with self.subTest(changed=changed):
                with f.repo.transaction() as db:
                    db.execute(
                        "UPDATE owned_agent_contracts SET output_json=? WHERE job_id=? AND role='arabic'",
                        (changed, f.job),
                    )
                with (
                    f.repo.connect() as db,
                    self.assertRaisesRegex(
                        ContractViolation,
                        "CONTRACT_ARABIC_EVIDENCE_INVALID",
                        msg="guard:arabic-delivery-proof",
                    ),
                ):
                    require_delivery_contract(db, f.job, f.result)
        with f.repo.transaction() as db:
            db.execute(
                "UPDATE owned_agent_contracts SET output_json=? WHERE job_id=? AND role='arabic'",
                (original, f.job),
            )
        with f.repo.connect() as db:
            require_delivery_contract(db, f.job, f.result)

    def test_arabic_reconciliation_rechecks_source_lineage(self):
        f = self.f
        self.language()
        with f.repo.transaction() as db:
            db.execute(
                "UPDATE owned_agent_contracts SET output_sha=? WHERE job_id=? AND role='fact_source'",
                ("f" * 64, f.job),
            )
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_DEPENDENCY_INVALID", msg="guard:arabic-reconcile-lineage"
        ):
            reconcile_stage(f.repo, f.job, f.token, 1, "arabic", 1)


@unittest.skipUnless(os.name == "posix", "Linux service integration required")
class ArabicServiceTests(unittest.TestCase):
    def test_language_rejection_never_reaches_render_or_transport(self):
        from test_owned_contract_adapter import AdapterServiceTests

        a = AdapterServiceTests()
        a.setUp()
        self.addCleanup(a.doCleanups)
        with (
            patch(
                "snap_bot.service.check_content",
                side_effect=ValueError("synthetic Arabic rejection"),
            ),
            self.assertRaisesRegex(
                ContractViolation, "CONTRACT_ARABIC_REJECTED", msg="guard:arabic-service-binding"
            ),
        ):
            a.execute()
        self.assertEqual(a.render_calls, 0, msg="guard:arabic-service-binding")
        self.assertEqual(a.calls, [])
        self.assertEqual(contract_status(a.f.repo, a.f.job, 1, "arabic"), "FAILED")
