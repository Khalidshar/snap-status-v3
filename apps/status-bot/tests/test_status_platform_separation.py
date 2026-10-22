"""Actual current policy: no historical-fixture guard overrides in these tests."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from status_bot import actions_preview, chat_probe, manual_transport, telegram_egress
from status_bot.platform_policy import PlatformSeparationError


class PlatformSeparationTests(unittest.TestCase):
    def blocked(self, callback):
        with self.assertRaisesRegex(
            PlatformSeparationError, "^STATUS_TELEGRAM_DISABLED_BY_PLATFORM_POLICY$"
        ):
            callback()

    def test_sender_cannot_use_default_or_injected_transport(self):
        transport = Mock(side_effect=AssertionError("No transport call permitted"))
        config = Mock()
        self.blocked(lambda: telegram_egress.deliver(config, "job", "a" * 64))
        self.blocked(lambda: telegram_egress.deliver(config, "job", "a" * 64, transport))
        self.blocked(
            lambda: telegram_egress._deliver(
                config, "job", "a" * 64, transport, None, "HUMAN_FRAME"
            )
        )
        transport.assert_not_called()
        self.assertEqual(config.mock_calls, [])

    def test_http_methods_are_blocked_before_dns_or_connection(self):
        with (
            patch.object(telegram_egress, "validate_target") as dns,
            patch.object(telegram_egress, "PinnedHTTPSConnection") as connect,
        ):
            for method in ("getMe", "getChat", "sendVideo"):
                self.blocked(lambda: telegram_egress.api("", method))
            dns.assert_not_called()
            connect.assert_not_called()

    def test_actions_cannot_read_github_or_send_even_with_approval(self):
        env = {"HUMAN_APPROVAL": "HUMAN_FRAME"}
        get = Mock()
        transport = Mock()
        self.blocked(lambda: actions_preview.preflight(env, get))
        self.blocked(lambda: actions_preview.send_bundle(Path("/missing"), env, get, transport))
        get.assert_not_called()
        transport.assert_not_called()

    def test_no_new_intent_or_changed_existing_intent(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "send-intent"
            self.blocked(lambda: actions_preview.write_intent({}, target))
            self.assertFalse(target.exists())
            target.mkdir()
            intent = target / "intent.json"
            original = b'{"state":"EXTERNAL_EFFECT_INTENT","historical":true}'
            intent.write_bytes(original)
            self.blocked(lambda: actions_preview.write_intent({}, target))
            self.assertEqual(intent.read_bytes(), original)

    def test_unix_client_and_server_cannot_connect_or_bind(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(manual_transport, "UnixHTTPConnection") as connect,
        ):
            socket = Path(folder) / "egress.sock"
            client = manual_transport.Client(socket, 0)
            self.blocked(lambda: client.telegram("", "sendVideo"))
            caller = Mock()
            self.blocked(lambda: manual_transport.Transport(socket, 0, "", "42", "a" * 64, caller))
            self.assertFalse(socket.exists())
            connect.assert_not_called()
            caller.assert_not_called()

    def test_probe_cannot_reuse_snap_identity(self):
        caller = Mock()
        self.blocked(lambda: chat_probe.probe({}, caller))
        with patch.object(chat_probe, "validate_target") as dns:
            self.blocked(lambda: chat_probe.readonly_api("", "getMe"))
            dns.assert_not_called()
        caller.assert_not_called()

    def test_environment_switch_cannot_enable_legacy_channel(self):
        with patch.dict(
            "os.environ",
            {
                "ALLOW_STATUS_TELEGRAM": "true",
                "HUMAN_APPROVAL": "HUMAN_FRAME",
                "STATUS_DELIVERY_CHANNEL": "telegram",
            },
        ):
            self.blocked(lambda: telegram_egress.deliver(None, "job", "a" * 64))


if __name__ == "__main__":
    unittest.main()
