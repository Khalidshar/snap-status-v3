import copy
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from snap_bot.n1_contracts import Ledger, Refused, recovery_gate
from studio_core.config import Config
from studio_core.jobs import LeaseLost, Repository


class Clock:
    def __init__(self):
        self.now = 1000

    def __call__(self):
        return self.now


class N1Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cfg = Config.load(Path(__file__).parents[3] / "config/snap/weather.toml")
        self.clock = Clock()
        self.config = replace(cfg, data_root=Path(self.temp.name))
        self.repo = Repository(self.config, clock=self.clock)
        self.job, _ = self.repo.enqueue("p0", "owner", "weather", "NORMAL", "input", 5000)
        self.claim = self.repo.claim()
        self.ledger = Ledger(self.repo, b"p0-synthetic-key-not-production-00000000")

    def begin(self, claim=None, **kw):
        claim = claim or self.claim
        return self.ledger.begin(
            claim["id"],
            claim["lease_token"],
            claim["attempt"],
            kw.get("agent", "fact_source"),
            kw.get("status", "RUNNING"),
            kw.get("event_id", "source_start"),
            kw.get("payload_sha", "a" * 64),
        )

    def confirm(self, receipt, claim=None):
        claim = claim or self.claim
        return self.ledger.confirm(claim["id"], claim["lease_token"], claim["attempt"], receipt)

    def reclaim(self):
        # Model an administrative lease revocation without advancing intent deadline.
        with self.repo.transaction() as db:
            db.execute(
                "UPDATE jobs SET lease_until=? WHERE id=?", (self.clock() - 1, self.claim["id"])
            )
        return self.repo.claim(job_id=self.claim["id"])

    def gate(self, claim=None, enabled=True):
        claim = claim or self.claim
        return recovery_gate(
            self.repo, claim["id"], claim["lease_token"], claim["attempt"], enabled=enabled
        )

    def test_initial_success_idempotency_and_payload_conflict(self):
        envelope = self.begin()
        self.assertEqual(self.begin(), envelope)
        with self.assertRaises(Refused):
            self.begin(payload_sha="b" * 64)
        receipt = self.ledger.simulated_writer_receipt(envelope)
        self.confirm(receipt)
        self.assertEqual(self.confirm(receipt), receipt)
        self.assertEqual(self.ledger.view(self.job["id"]), [receipt])

    def test_begin_rejects_old_owner(self):
        self.reclaim()
        with self.assertRaises(LeaseLost, msg="guard:begin owner"):
            self.begin()

    def test_confirm_rejects_old_owner_after_real_claim(self):
        envelope = self.begin()
        receipt = self.ledger.simulated_writer_receipt(envelope)
        ready = threading.Event()
        resume = threading.Event()
        outcome = []

        def old():
            ready.set()
            resume.wait(5)
            try:
                self.confirm(receipt)
                outcome.append("accepted")
            except LeaseLost:
                outcome.append("stale")

        thread = threading.Thread(target=old)
        thread.start()
        self.assertTrue(ready.wait(5))
        new = self.reclaim()
        self.assertEqual(new["attempt"], 2)
        resume.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ["stale"], msg="guard:confirm owner")
        self.assertEqual(self.ledger.view(self.job["id"]), [])

    def test_seal_blocks_late_confirmation_and_preserves_lock_independence(self):
        envelope = self.begin()
        receipt = self.ledger.simulated_writer_receipt(envelope)
        new = self.ledger.seal(envelope["body"]["gen"])
        self.assertNotEqual(new, envelope["body"]["gen"])
        with self.assertRaises(Refused, msg="guard:sealed generation"):
            self.confirm(receipt)
        self.assertEqual(self.ledger.view(self.job["id"]), [])
        self.assertEqual(self.begin(event_id="second")["body"]["gen"], new)

    def test_current_view_excludes_old_confirmed_attempts(self):
        self.confirm(self.ledger.simulated_writer_receipt(self.begin()))
        new = self.reclaim()
        self.assertEqual(self.ledger.view(self.job["id"]), [], msg="guard:current epoch view")
        env = self.begin(new, event_id="new_start")
        receipt = self.ledger.simulated_writer_receipt(env)
        self.confirm(receipt, new)
        self.assertEqual(self.ledger.view(self.job["id"]), [receipt])

    def test_all_bound_fields_are_authenticated(self):
        envelope = self.begin()
        for field in envelope["body"]:
            with self.subTest(field=field):
                forged = copy.deepcopy(envelope)
                forged["body"][field] = "tampered"
                with self.assertRaises(Refused):
                    self.ledger.simulated_writer_receipt(forged)

    def test_modified_receipt_and_forged_high_attempt_rejected(self):
        envelope = self.begin()
        receipt = self.ledger.simulated_writer_receipt(envelope)
        forged = copy.deepcopy(receipt)
        forged["envelope"]["body"]["attempt"] = 99
        with self.assertRaises(Refused):
            self.confirm(forged)
        with self.assertRaises(LeaseLost):
            self.ledger.begin(
                self.claim["id"],
                self.claim["lease_token"],
                99,
                "fact_source",
                "RUNNING",
                "forged",
                "b" * 64,
            )
        self.confirm(receipt)

    def test_full_history_prevents_reclaim_bypass(self):
        for state in ("RESEARCHING", "PROCESSING", "DOWNLOADING", "DESIGNING", "RENDERING"):
            self.repo.transition(self.claim["id"], self.claim["lease_token"], state)
        second = self.reclaim()
        self.assertEqual(second["recovered_from"], "RENDERING")
        # Intentionally do NOT write FAILED. Third claim reports RUNNING.
        third = self.reclaim()
        self.assertEqual(third["recovered_from"], "RUNNING")
        with self.assertRaisesRegex(Refused, "RECOVERY_REFUSED", msg="guard:full history"):
            self.gate(third)

    def test_claim_cap_survives_failed_refusal_persistence(self):
        for epoch in range(1, 6):
            claim = self.claim if epoch == 1 else self.reclaim()
            self.assertEqual(claim["attempt"], epoch)
            if epoch <= 3:
                self.assertTrue(self.gate(claim))
            else:
                with self.assertRaisesRegex(Refused, "ATTEMPTS_EXHAUSTED", msg="guard:claim cap"):
                    self.gate(claim)

    def test_disabled_flag_and_missing_history_fail_closed(self):
        new = self.reclaim()
        with self.assertRaisesRegex(Refused, "DISABLED"):
            self.gate(new, enabled=False)
        with self.repo.transaction() as db:
            db.execute(
                "UPDATE events SET details='{}' WHERE job_id=? AND event='job.claimed'",
                (self.claim["id"],),
            )
        with self.assertRaises(Refused):
            self.gate(new)

    def test_prior_design_intent_refuses_recovery(self):
        self.begin(agent="design")
        new = self.reclaim()
        with self.assertRaisesRegex(Refused, "RECOVERY_REFUSED"):
            self.gate(new)

    def test_append_wait_holds_no_db_writer_lock(self):
        envelope = self.begin()
        ready = threading.Event()
        resume = threading.Event()
        outcome = []

        def writer():
            ready.set()
            resume.wait(5)
            outcome.append(self.ledger.simulated_writer_receipt(envelope))

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(ready.wait(5))
            # No timing/sleep assertion: real independent SQLite writer can finish.
            self.repo.heartbeat(self.claim["id"], self.claim["lease_token"])
            self.assertTrue(thread.is_alive())
        finally:
            resume.set()
            thread.join(5)
        self.assertEqual(len(outcome), 1)
        self.confirm(outcome[0])

    def test_same_contract_for_both_application_ids(self):
        for app in ("snap-bot-v2", "status-bot-v2"):
            with self.subTest(app=app):
                cfg = replace(self.config, app=app, data_root=Path(self.temp.name) / app)
                repo = Repository(cfg, clock=self.clock)
                job, _ = repo.enqueue("same", "owner", "weather", "NORMAL", "input", 5000)
                claim = repo.claim()
                ledger = Ledger(repo, b"p0-synthetic-key-not-production-00000000")
                env = ledger.begin(
                    job["id"], claim["lease_token"], 1, "fact_source", "RUNNING", "first", "a" * 64
                )
                self.assertEqual(env["body"]["app"], app)
                receipt = ledger.simulated_writer_receipt(env)
                ledger.confirm(job["id"], claim["lease_token"], 1, receipt)
                self.assertEqual(ledger.view(job["id"]), [receipt])

    def test_receipt_expiry_rejected(self):
        envelope = self.begin()
        self.clock.now = envelope["body"]["not_after"]
        with self.assertRaises(Refused):
            self.ledger.simulated_writer_receipt(envelope)


if __name__ == "__main__":
    unittest.main()
