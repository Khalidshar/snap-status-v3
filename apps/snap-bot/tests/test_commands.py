import base64
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from snap_bot.command_transport import Client, Transport, normalize_updates, validated_video
from snap_bot.command_worker import accept_batch, initialize, network_guard
from snap_bot.telegram_egress import EXPECTED_BOT, multipart
from studio_core.config import Config
from studio_core.jobs import Repository
from studio_core.jobs.gateway import CommandGateway


class CommandTests(unittest.TestCase):
    def update(self, uid=1, actor=42, chat=42, kind="private", text="/weather", date=1000):
        return {
            "update_id": uid,
            "message": {
                "date": date,
                "text": text,
                "from": {"id": actor},
                "chat": {"id": chat, "type": kind},
            },
        }

    def test_owner_private_command_and_suffix(self):
        result = normalize_updates([self.update(text="/weather@" + EXPECTED_BOT)], "42", 1001)
        self.assertEqual(result["offset"], 2)
        self.assertEqual(result["commands"][0]["command"], "weather")

    def test_worker_refuses_missing_kernel_network_isolation(self):
        with self.assertRaisesRegex(RuntimeError, "isolation"):
            network_guard()

    def test_other_user_group_stale_and_future_filtered(self):
        updates = [
            self.update(1, actor=99),
            self.update(2, chat=99),
            self.update(3, kind="group"),
            self.update(4, date=99),
            self.update(5, date=2000),
        ]
        result = normalize_updates(updates, "42", 1000)
        self.assertEqual(result["commands"], [])
        self.assertEqual(result["offset"], 6)

    def test_arbitrary_text_arguments_and_wrong_bot_filtered(self):
        for text in ["/weather Cairo", "/weather@other_bot", "hello", "/shell", "/weather\n/start"]:
            self.assertEqual(
                normalize_updates([self.update(text=text)], "42", 1001)["commands"], []
            )

    def test_multipart_owner_fixed_and_fields_rebuilt(self):
        body, kind = multipart("42", b"mp4")
        rebuilt, newkind = validated_video(body, kind, "42")
        self.assertIn(b"mp4", rebuilt)
        with self.assertRaisesRegex(ValueError, "owner"):
            validated_video(body, kind, "99")

    def test_multipart_duplicate_destinations_rejected(self):
        body, kind = multipart("42", b"mp4")
        boundary = kind.split("boundary=")[1]
        attack = body.replace(
            ("--" + boundary + "--").encode(),
            (
                "--"
                + boundary
                + '\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n99\r\n--'
                + boundary
                + "--"
            ).encode(),
        )
        with self.assertRaises(ValueError):
            validated_video(attack, kind, "42")

    def test_durable_cursor_and_same_update_does_not_duplicate_job(self):
        with tempfile.TemporaryDirectory() as temp:
            config = replace(
                Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"),
                data_root=Path(temp),
                owners={"telegram": ("42",)},
            )
            repo = Repository(config)
            gateway = CommandGateway(config, repo, "s" * 32)
            initialize(repo)
            batch = normalize_updates([self.update()], "42", 1001)
            first = accept_batch(repo, gateway, batch, "42")
            second = accept_batch(repo, gateway, batch, "42")
            self.assertTrue(first[0]["created"])
            self.assertFalse(second[0]["created"])
            self.assertEqual(first[0]["job_id"], second[0]["job_id"])
            with repo.connect() as db:
                self.assertEqual(db.execute("SELECT offset FROM telegram_cursor").fetchone()[0], 2)

    def test_unknown_rpc_cannot_become_generic_network_proxy(self):
        with tempfile.TemporaryDirectory() as temp:
            caller = Mock()
            with Transport(
                Path(temp) / "rpc.sock", Path(temp) / "state", "unused", "42", caller
            ) as server:
                with self.assertRaises(ValueError):
                    server.handle(
                        "/api",
                        {"method": "deleteWebhook", "body": "", "content_type": "application/json"},
                    )
                with self.assertRaises(ValueError):
                    server.handle("/fetch", {"url": "https://evil.example"})
            caller.assert_not_called()

    def test_message_send_unknown_is_not_retried_after_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            caller = Mock(side_effect=TimeoutError())
            with Transport(path / "rpc.sock", path / "state", "unused", "42", caller) as server:
                with self.assertRaises(TimeoutError):
                    server.handle("/reply", {"key": "1:reply", "message": "help"})
            with Transport(path / "rpc.sock", path / "state", "unused", "42", caller) as server:
                self.assertEqual(
                    server.handle("/reply", {"key": "1:reply", "message": "help"}),
                    {"replayed": False},
                )
            self.assertEqual(caller.call_count, 1)


if __name__ == "__main__":
    unittest.main()
