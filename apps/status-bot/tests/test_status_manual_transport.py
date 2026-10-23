import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from status_bot.manual_transport import Client, Transport, approved_video
from status_bot.telegram_egress import EXPECTED_BOT, multipart


class ManualTransportTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch

        policy = patch("status_bot.platform_policy.reject_status_telegram", new=lambda: None)
        policy.start()
        self.addCleanup(policy.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "egress.sock"
        self.calls = []
        self.video = b"synthetic-video"
        self.digest = hashlib.sha256(self.video).hexdigest()

        def caller(token, method, *args):
            self.calls.append(method)
            if method == "getMe":
                return {"username": EXPECTED_BOT}
            if method == "getChat":
                return {"id": 42, "type": "private"}
            return {"message_id": 8, "chat": {"id": 42}}

        self.server = Transport(
            self.path, os.getuid(), "synthetic-token", "42", self.digest, caller
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_identity_and_owner_and_pinned_video(self):
        client = Client(self.path, os.getuid())
        self.assertEqual(client.telegram("", "getMe")["username"], EXPECTED_BOT)
        client.telegram("", "getChat", json.dumps({"chat_id": "42"}).encode())
        body, kind = multipart("42", self.video)
        self.assertEqual(client.telegram("", "sendVideo", body, kind)["message_id"], 8)
        self.assertEqual(self.calls, ["getMe", "getChat", "sendVideo"])

    def test_wrong_server_uid_before_external_effect(self):
        with self.assertRaises(ValueError):
            Client(self.path, os.getuid() + 10000).telegram("", "getMe")
        self.assertEqual(self.calls, [])

    def test_wrong_caller_uid_before_external_effect(self):
        self.server.worker_uid = os.getuid() + 10000
        with self.assertRaises(Exception):
            Client(self.path, os.getuid()).telegram("", "getMe")
        self.assertEqual(self.calls, [])

    def test_unapproved_digest_and_destination(self):
        for owner, video in [("99", self.video), ("42", b"changed")]:
            body, kind = multipart(owner, video)
            with self.assertRaises(ValueError):
                approved_video(body, kind, "42", self.digest)
        self.assertEqual(self.calls, [])

    def test_no_poll_or_commands_or_arbitrary_fields(self):
        for method in ("getUpdates", "setMyCommands", "sendMessage"):
            with self.assertRaises(ValueError):
                self.server.handle(
                    {"method": method, "body": "", "content_type": "application/json"}
                )
        with self.assertRaises(ValueError):
            self.server.handle(
                {
                    "method": "getMe",
                    "body": "",
                    "content_type": "application/json",
                    "url": "https://example.com",
                }
            )
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
