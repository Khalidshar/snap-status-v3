import json
import time
import unittest
from unittest.mock import patch

import test_telegram_delivery as existing

from snap_bot.did_you_know import load_facts, make_content
from snap_bot.telegram_egress import deliver, multipart
from studio_core.storage import Storage


class FactDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = existing.DeliveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        root = Storage(f.config).workspace(f.job)
        # This fixture represents an explicitly designated pre-Adapter preview.
        # Production never infers or backfills this marker from missing contracts.
        with f.repo.transaction() as db:
            db.execute("UPDATE jobs SET command='did_you_know' WHERE id=?", (f.job,))
            db.execute(
                "UPDATE events SET details=? WHERE job_id=? AND event='job.queued'",
                (
                    json.dumps(
                        {
                            "command": "did_you_know",
                            "priority": "NORMAL",
                            "contract_policy": "legacy-preview-before-adapter-v1",
                        }
                    ),
                    f.job,
                ),
            )
        content = make_content(load_facts()[0], time.time())
        (root / "input/content.json").write_text(json.dumps(content), encoding="utf-8")
        result = json.loads((root / "output/render-result.json").read_text())
        result["content_hash"] = content["content_hash"]
        result["content_type"] = "did_you_know"
        result["safe_area"] = {"result": "PASS"}
        (root / "output/render-result.json").write_text(json.dumps(result))
        (root / "output/weather.mp4").rename(root / "output/did-you-know.mp4")

    def test_manual_review_required_before_any_network(self):
        f = self.fixture
        with self.assertRaisesRegex(ValueError, "human preview"):
            deliver(f.config, f.job, f.digest, f.transport)
        self.assertEqual(f.calls, [])

    def test_manual_fact_delivery_targets_test_bot_and_sends_once(self):
        f = self.fixture
        for _ in range(2):
            self.assertEqual(
                deliver(f.config, f.job, f.digest, f.transport, review_mode="HUMAN_FRAME")[
                    "delivery"
                ],
                "PUBLISHED",
            )
        self.assertEqual(f.calls.count("sendVideo"), 1)

    def test_new_unmarked_fact_cannot_contact_telegram_without_contracts(self):
        f = self.fixture
        with f.repo.transaction() as db:
            db.execute(
                "UPDATE events SET details=? WHERE job_id=? AND event='job.queued'",
                (json.dumps({"command": "did_you_know"}), f.job),
            )
        from snap_bot.owned_contract_adapter import ContractViolation

        with self.assertRaisesRegex(ContractViolation, "CONTRACT_DELIVERY_BLOCKED"):
            deliver(f.config, f.job, f.digest, f.transport, review_mode="HUMAN_FRAME")
        self.assertEqual(f.calls, [])

    def test_fact_send_timeout_keeps_unknown_and_never_sends_again(self):
        f = self.fixture

        def interrupted(token, method, *args):
            if method == "sendVideo":
                f.calls.append(method)
                raise TimeoutError("synthetic disconnect after accepting request; ACK lost")
            return f.transport(token, method, *args)

        with self.assertRaisesRegex(RuntimeError, "DELIVERY_UNKNOWN"):
            deliver(f.config, f.job, f.digest, interrupted, review_mode="HUMAN_FRAME")
        from studio_core.jobs import LeaseLost

        try:
            retried = deliver(f.config, f.job, f.digest, f.transport, review_mode="HUMAN_FRAME")
        except LeaseLost:
            self.fail(
                "guard:unknown-fact-no-resend: unknown evidence was bypassed and a new claim was attempted"
            )
        self.assertEqual(
            retried["delivery"], "DELIVERY_UNKNOWN", msg="guard:unknown-fact-no-resend"
        )
        self.assertEqual(f.calls.count("sendVideo"), 1, msg="guard:unknown-fact-no-resend")
        with f.repo.connect() as db:
            self.assertEqual(
                db.execute("SELECT state FROM deliveries WHERE job_id=?", (f.job,)).fetchone()[0],
                "DELIVERY_UNKNOWN",
                msg="guard:unknown-fact-evidence",
            )

    def test_wrong_bot_rejected_before_video(self):
        f = self.fixture

        def transport(token, method, *args):
            if method == "getMe":
                return {"username": "another_bot"}
            return f.transport(token, method, *args)

        with self.assertRaises(ValueError):
            deliver(f.config, f.job, f.digest, transport, review_mode="HUMAN_FRAME")
        self.assertNotIn("sendVideo", f.calls)

    def test_emoji_is_caption_only(self):
        body, _ = multipart(42, b"synthetic-video", "did_you_know")
        self.assertIn("تابعني للمزيد 👻".encode(), body)
        self.assertIn(b"did-you-know.mp4", body)

    def test_execute_does_not_autodeliver_facts(self):
        from snap_bot.service import execute

        f = self.fixture
        with (
            patch("snap_bot.service.expire_pending_adoptions"),
            self.assertRaisesRegex(ValueError, "separate manual"),
        ):
            execute(
                f.config,
                {},
                "synthetic-secret-at-least-32-characters",
                claim={"id": f.job, "command": "did_you_know"},
                delivery=lambda *args: self.fail("auto send"),
            )


if __name__ == "__main__":
    unittest.main()
