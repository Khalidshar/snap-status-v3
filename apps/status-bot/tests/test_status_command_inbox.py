"""Authenticated ingress, durable claims and uncertain-send contracts. No live sends."""

import hashlib
import hmac
import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_status_whatsapp_outbox as fixtures

from status_bot.command_inbox import claim
from status_bot.owner_commands import answer, process_one
from status_bot.whatsapp_contract import DeliveryBlocked
from status_bot.whatsapp_egress import GraphCredentials, GraphTransport


class CommandInboxTests(unittest.TestCase):
    setUp = fixtures.WhatsAppContracts.setUp

    def post(self, text="الحالة", *, uid="ONE", age=0, outsider=False, bad_signature=False):
        raw, _ = fixtures.WhatsAppContracts.webhook(self, inbound=True)
        payload = json.loads(raw)
        message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
        message.update(id="wamid." + uid, timestamp=str(int(time.time()) - age))
        message["text"]["body"] = text
        if outsider:
            message["from"] = "19999999999"
        raw = json.dumps(payload, ensure_ascii=False).encode()
        signature = "sha256=" + hmac.new(self.identity.webhook_key, raw, hashlib.sha256).hexdigest()
        return self.outbox.webhook(raw, "sha256=" + "0" * 64 if bad_signature else signature)

    def rows(self):
        with self.repo.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM whatsapp_commands")]

    def test_signed_owner_command_is_committed_once_across_replayed_webhooks(self):
        self.post()
        self.post()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "الحالة")
        self.assertNotIn("wamid.ONE", repr(rows))
        with ThreadPoolExecutor(2) as pool:
            claimed = list(pool.map(lambda _: claim(self.repo, self.identity), range(2)))
        self.assertEqual(sum(row is not None for row in claimed), 1)
        self.assertIsNone(claim(self.repo, self.identity))

    def test_unauthenticated_foreign_and_old_messages_cannot_execute(self):
        with self.assertRaises(DeliveryBlocked):
            self.post(bad_signature=True)
        self.post(uid="FOREIGN", outsider=True)
        self.post(uid="OLD", age=901)
        self.assertEqual(self.rows(), [])

    def test_inbox_failure_rolls_back_window_and_returns_no_success(self):
        with patch("status_bot.whatsapp_outbox.enqueue", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                self.post()
        self.assertEqual(self.rows(), [])
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM whatsapp_windows").fetchone()[0], 0)

    def test_burst_and_expired_queue_cannot_trigger_unbounded_work(self):
        for i in range(12):
            self.post(uid="CMD" + str(i))
        self.assertEqual(sum(r["state"] == "QUEUED" for r in self.rows()), 10)
        self.assertEqual(sum(r["state"] == "RATE_LIMITED" for r in self.rows()), 2)
        with self.repo.transaction() as db:
            db.execute("UPDATE whatsapp_commands SET at=at-901")
        self.assertIsNone(claim(self.repo, self.identity))

    def test_unknown_send_is_consumed_and_is_never_retried(self):
        self.post()
        transport = SimpleNamespace(send_text=Mock(side_effect=TimeoutError("fixture secret")))
        runtime = SimpleNamespace(events=lambda: [])
        self.assertTrue(process_one(self.outbox, transport, None, runtime))
        self.post()
        self.assertFalse(process_one(self.outbox, transport, None, runtime))
        self.assertEqual(transport.send_text.call_count, 1)
        self.assertEqual(self.rows()[0]["state"], "BLOCKED_OR_UNKNOWN")
        self.assertNotIn("fixture secret", repr(self.rows()))

    def test_valid_reply_and_news_reuse_verified_inbox_and_pinned_transport(self):
        self.post("أخبار السعودية")
        transport = fixtures.OfflineTransport(self.identity)
        transport.send_text = Mock(return_value=transport.send_video.return_value)
        news = SimpleNamespace(reply=lambda command, event, send: send("عنوان عربي") or True)
        self.assertTrue(process_one(self.outbox, transport, news, None))
        self.assertEqual(self.rows()[0]["state"], "ACK")
        transport.send_text.assert_called_once_with("عنوان عربي")
        transport.send_video.assert_not_called()

    def test_unimplemented_video_commands_do_not_issue_approvals(self):
        runtime = SimpleNamespace(events=lambda: [])
        for text in ("أرسل", "نعم", "دعاء", "آية اليوم"):
            self.assertIsInstance(answer(text, runtime), str)

    def test_graph_text_pins_destination_and_disables_url_preview(self):
        credentials = GraphCredentials(
            "v21.0", "12345002", "10000000000", "fixture-token-only-0000"
        )
        with patch("status_bot.whatsapp_egress.graph_post") as post:
            GraphTransport(credentials, enabled=True).send_text("عنوان عربي")
        args = post.call_args.args
        payload = json.loads(args[2])
        self.assertEqual(payload["to"], credentials.recipient)
        self.assertEqual(payload["text"], {"body": "عنوان عربي", "preview_url": False})
        self.assertEqual(args[1], "messages")
