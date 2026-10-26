"""The owner's manual WhatsApp cycle on the server: approve one SHA, send it once.

Graph is always the offline transport here; identities and keys are fictitious.
"""

import hashlib
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_status_whatsapp_outbox as fixtures

from status_bot import server_preview as server
from status_bot.whatsapp_contract import ApiResponse, json_bytes


@unittest.skipUnless(os.name == "posix", "Linux locking and fsync")
class ManualWhatsAppCycle(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.WhatsAppContracts()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        identity = self.base.identity
        self.env = {
            "WA_TOKEN": "unit-fixture-token-not-real-0000",
            "STATUS_WA_BUSINESS_ACCOUNT": identity.business_account,
            "STATUS_WA_PHONE_NUMBER": identity.phone_number,
            "STATUS_WA_RECIPIENT": identity.recipient,
            "STATUS_WA_GATE_KEY": identity.gate_key.decode(),
            "STATUS_WA_WEBHOOK_KEY": identity.webhook_key.decode(),
        }
        value, root, _manifest, _review, _signature = self.base.fixture("wa_manual")
        claim = value["claim"]
        self.job = claim["id"]
        video = (root / "output/weather.mp4").read_bytes()
        self.sha = hashlib.sha256(video).hexdigest()
        self.runtime = server.Runtime(self.base.root / "status")
        preview_id, run = "a" * 32, "b" * 12
        preview = self.runtime.root / "previews" / preview_id
        preview.mkdir(parents=True)
        (self.runtime.root / "runs" / run).mkdir(parents=True)
        (preview / "weather.mp4").write_bytes(video)
        (preview / "lease.json").write_text(
            json.dumps({"run": run, "lease_token": claim["lease_token"], "attempt": 1})
        )
        self.runtime.append(
            "RENDERED",
            preview_id=preview_id,
            job_id=self.job,
            video_sha=self.sha,
            expires_at=time.time() + 600,
        )
        self.transport = fixtures.OfflineTransport(identity)
        load = patch.object(server.Config, "load", return_value=self.base.cfg)
        load.start()
        self.addCleanup(load.stop)

    def send(self, env=None):
        return self.runtime.send(self.sha, self.transport, env or self.env)

    def kinds(self):
        return [e["kind"] for e in self.runtime.events()]

    def test_approved_sha_is_sent_once(self):
        self.runtime.approve(self.sha, 0)
        ack = self.send()
        self.assertEqual(ack["kind"], "ACK")
        self.assertEqual(ack["state"], "API_ACCEPTED")
        self.transport.upload_video.assert_called_once()
        self.transport.send_video.assert_called_once()
        self.assertEqual(self.kinds(), ["RENDERED", "HUMAN_FRAME", "INTENT", "ACK"])
        with self.assertRaises(ValueError):
            self.send()
        self.transport.send_video.assert_called_once()
        self.assertNotIn(self.env["WA_TOKEN"], (self.runtime.root / "events.jsonl").read_text())

    def test_no_send_without_owner_approval(self):
        with self.assertRaises(ValueError, msg="guard:owner-approval"):
            self.send()
        self.transport.upload_video.assert_not_called()

    def test_approval_needs_a_recent_message_and_is_single(self):
        for hours in (-1, 24, "1", None):
            with self.subTest(hours=hours), self.assertRaises(ValueError):
                self.runtime.approve(self.sha, hours)
        self.runtime.approve(self.sha, 3)
        with self.assertRaises(ValueError, msg="guard:single-approval"):
            self.runtime.approve(self.sha, 0)

    def test_closed_window_stops_before_any_intent(self):
        self.runtime.approve(self.sha, 23)
        later = time.time() + 3601
        with patch.object(server.time, "time", return_value=later), self.assertRaises(ValueError):
            self.send()
        self.assertNotIn("INTENT", self.kinds(), "guard:window-before-intent")
        self.transport.upload_video.assert_not_called()

    def test_missing_environment_stops_before_any_intent(self):
        self.runtime.approve(self.sha, 0)
        env = dict(self.env)
        del env["WA_TOKEN"]
        with self.assertRaises(ValueError):
            self.send(env)
        self.assertNotIn("INTENT", self.kinds())

    def test_graph_refusal_is_recorded_and_final(self):
        self.runtime.approve(self.sha, 0)
        refusal = json_bytes({"error": {"code": 131047, "message": "unit fixture"}})
        self.transport.send_video = Mock(return_value=ApiResponse(400, refusal))
        with self.assertRaises(RuntimeError):
            self.send()
        self.assertEqual(self.kinds()[-1], "REJECTED")
        self.assertEqual(self.runtime.events()[-1]["code"], "WA_API_REJECTED_131047")
        with self.assertRaises(ValueError, msg="guard:no-retry"):
            self.send()

    def test_unknown_outcome_blocks_the_sha(self):
        self.runtime.approve(self.sha, 0)
        self.transport.send_video = Mock(side_effect=TimeoutError("unit fixture"))
        with self.assertRaises(RuntimeError):
            self.send()
        self.assertEqual(self.kinds()[-1], "UNKNOWN")
        with self.assertRaises(ValueError):
            self.send()
        self.transport.send_video.assert_called_once()

    def test_changed_preview_is_refused(self):
        self.runtime.approve(self.sha, 0)
        preview = self.runtime.root / "previews" / ("a" * 32) / "weather.mp4"
        preview.write_bytes(b"changed")
        with self.assertRaises(ValueError, msg="guard:preview-bytes"):
            self.send()


class RunConfigLease(unittest.TestCase):
    def test_server_jobs_stay_owned_for_the_content_hour(self):
        import tempfile

        from studio_core.config import Config

        with tempfile.TemporaryDirectory() as directory:
            config = Config.load(server.run_config(Path(directory)))
        self.assertEqual(config.lease_seconds, 3600)


if __name__ == "__main__":
    unittest.main()
