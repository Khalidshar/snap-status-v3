import json
import unittest
from unittest.mock import patch

from status_bot import chat_probe as probe


def environment():
    return {
        "GITHUB_REPOSITORY": probe.REPOSITORY,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_ACTOR": "Khalidshar",
        "GITHUB_TRIGGERING_ACTOR": "Khalidshar",
        "STATUS_TG_TEST_TOKEN": "123456:" + "x" * 35,
        "STATUS_TG_TEST_CHAT": "987654321",
    }


class ProbeTests(unittest.TestCase):
    def setUp(self):
        policy = patch("status_bot.platform_policy.reject_status_telegram", new=lambda: None)
        policy.start()
        self.addCleanup(policy.stop)

    def test_bot_id_is_not_a_human_recipient(self):
        call = unittest.mock.Mock(
            return_value={"is_bot": True, "username": probe.EXPECTED_BOT, "id": 987654321}
        )
        result = probe.probe(environment(), call)
        self.assertEqual(result["code"], "BOT_ID_USED_AS_CHAT")
        self.assertTrue(result["bot_identity_verified"])
        self.assertFalse(result["private_chat_verified"])
        self.assertFalse(result["send_video_started"])
        self.assertEqual(call.call_count, 1)
        self.assertNotIn(environment()["STATUS_TG_TEST_CHAT"], json.dumps(result))

    def test_chat_response_identifying_bot_is_rejected(self):
        for chat in [
            {"type": "private", "id": 987654321, "username": probe.EXPECTED_BOT},
            {"type": "private", "id": 987654321, "is_bot": True},
        ]:
            call = unittest.mock.Mock(
                side_effect=[{"is_bot": True, "username": probe.EXPECTED_BOT, "id": 123456}, chat]
            )
            result = probe.probe(environment(), call)
            self.assertEqual(result["code"], "BOT_ID_USED_AS_CHAT")
            self.assertFalse(result["passed"])
            self.assertFalse(result["private_chat_verified"])
            self.assertFalse(result["send_video_started"])

    def test_success_only_reads(self):
        env = environment()
        calls = []

        def call(token, method, body=b""):
            calls.append(method)
            self.assertEqual(token, env["STATUS_TG_TEST_TOKEN"])
            if method == "getMe":
                return {"is_bot": True, "username": probe.EXPECTED_BOT}
            self.assertEqual(json.loads(body), {"chat_id": int(env["STATUS_TG_TEST_CHAT"])})
            return {"type": "private", "id": int(env["STATUS_TG_TEST_CHAT"])}

        result = probe.probe(env, call)
        self.assertTrue(result["passed"])
        self.assertEqual(calls, ["getMe", "getChat"])
        for field in ("send_message_started", "send_video_started", "send_intent_created"):
            self.assertIs(result[field], False)
        text = json.dumps(result)
        self.assertNotIn(env["STATUS_TG_TEST_TOKEN"], text)
        self.assertNotIn(env["STATUS_TG_TEST_CHAT"], text)

    def test_every_dispatch_guard(self):
        changes = {
            "GITHUB_REPOSITORY": "other/repo",
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": "refs/heads/other",
            "GITHUB_ACTOR": "other",
            "GITHUB_TRIGGERING_ACTOR": "other",
        }
        for key, value in changes.items():
            with self.subTest(key=key):
                env = environment()
                env[key] = value
                call = unittest.mock.Mock()
                result = probe.probe(env, call)
                self.assertEqual(result["code"], "DISPATCH_NOT_ALLOWED")
                call.assert_not_called()

    def test_bad_credentials_never_connect(self):
        cases = [
            ("STATUS_TG_TEST_TOKEN", "", "INVALID_TEST_TOKEN"),
            ("STATUS_TG_TEST_CHAT", "", "INVALID_PRIVATE_CHAT_ID"),
            ("STATUS_TG_TEST_CHAT", "bot_username", "INVALID_PRIVATE_CHAT_ID"),
            ("STATUS_TG_TEST_CHAT", "-123", "INVALID_PRIVATE_CHAT_ID"),
            ("STATUS_TG_TEST_CHAT", "00123", "INVALID_PRIVATE_CHAT_ID"),
            ("STATUS_TG_TEST_CHAT", str(2**52), "INVALID_PRIVATE_CHAT_ID"),
        ]
        for key, value, expected in cases:
            with self.subTest(value=value):
                env = environment()
                env[key] = value
                call = unittest.mock.Mock()
                result = probe.probe(env, call)
                self.assertEqual(result["code"], expected)
                self.assertFalse(result["network_started"])
                call.assert_not_called()

    def test_wrong_bot_blocks_chat_lookup(self):
        call = unittest.mock.Mock(return_value={"is_bot": True, "username": "other_bot"})
        result = probe.probe(environment(), call)
        self.assertEqual(result["code"], "BOT_IDENTITY_MISMATCH")
        self.assertEqual(call.call_count, 1)

    def test_non_bot_blocks_chat_lookup(self):
        call = unittest.mock.Mock(return_value={"is_bot": False, "username": probe.EXPECTED_BOT})
        self.assertEqual(probe.probe(environment(), call)["code"], "BOT_IDENTITY_MISMATCH")
        self.assertEqual(call.call_count, 1)

    def test_chat_not_found(self):
        def call(token, method, body=b""):
            if method == "getMe":
                return {"is_bot": True, "username": probe.EXPECTED_BOT}
            raise probe.ApiRejected(400, 400, "CHAT_NOT_FOUND")

        result = probe.probe(environment(), call)
        self.assertEqual(result["stage"], "TELEGRAM_GET_CHAT")
        self.assertEqual(result["code"], "CHAT_NOT_FOUND")
        self.assertEqual(result["http_status"], 400)
        self.assertTrue(result["bot_identity_verified"])
        self.assertFalse(result["send_video_started"])

    def test_arbitrary_errors_redacted(self):
        env = environment()
        call = unittest.mock.Mock(side_effect=RuntimeError(env["STATUS_TG_TEST_TOKEN"]))
        result = probe.probe(env, call)
        self.assertEqual(result["code"], "NETWORK_OR_RESPONSE_ERROR")
        self.assertNotIn(env["STATUS_TG_TEST_TOKEN"], json.dumps(result))

    def test_timeout(self):
        call = unittest.mock.Mock(side_effect=TimeoutError("secret"))
        self.assertEqual(probe.probe(environment(), call)["code"], "NETWORK_TIMEOUT")

    def test_private_chat_matches_exactly(self):
        for owner in (
            {"type": "group", "id": 987654321},
            {"type": "private", "id": 987654322},
            {"type": "private", "id": "987654321"},
        ):
            with self.subTest(owner=owner):
                call = unittest.mock.Mock(
                    side_effect=[{"is_bot": True, "username": probe.EXPECTED_BOT}, owner]
                )
                self.assertEqual(probe.probe(environment(), call)["code"], "PRIVATE_CHAT_MISMATCH")

    def test_transport_refuses_write_methods_before_network(self):
        for method in ("sendMessage", "sendVideo", "getUpdates", "deleteMessage"):
            with self.subTest(method=method), patch.object(probe, "validate_target") as target:
                with self.assertRaises(ValueError):
                    probe.readonly_api(environment()["STATUS_TG_TEST_TOKEN"], method)
                target.assert_not_called()

    def test_http_error_classification_never_retains_description(self):
        for description, expected in [
            ("Bad Request: chat not found", "CHAT_NOT_FOUND"),
            ("Unauthorized", "TOKEN_NOT_AUTHORIZED"),
            (environment()["STATUS_TG_TEST_TOKEN"], "TELEGRAM_API_REJECTED"),
        ]:
            with self.subTest(expected=expected):
                response = unittest.mock.Mock(status=400)
                response.read.return_value = json.dumps(
                    {"ok": False, "error_code": 400, "description": description}
                ).encode()
                connection = unittest.mock.Mock()
                connection.getresponse.return_value = response
                with (
                    patch.object(
                        probe,
                        "validate_target",
                        return_value=("api.telegram.org", "address", "/path"),
                    ),
                    patch.object(probe, "PinnedHTTPSConnection", return_value=connection),
                ):
                    with self.assertRaises(probe.ApiRejected) as caught:
                        probe.readonly_api(environment()["STATUS_TG_TEST_TOKEN"], "getChat", b"{}")
                self.assertEqual(caught.exception.classification, expected)
                self.assertNotIn(description, str(caught.exception))
                connection.close.assert_called_once()

    def test_exception_fields_are_fixed_and_bounded(self):
        error = probe.ApiRejected("secret", True, "server-provided-text")
        self.assertIsNone(error.status)
        self.assertIsNone(error.error_code)
        self.assertEqual(error.classification, "TELEGRAM_API_REJECTED")

    def test_response_limit(self):
        response = unittest.mock.Mock(status=200)
        response.read.return_value = b"x" * 65_537
        connection = unittest.mock.Mock()
        connection.getresponse.return_value = response
        with (
            patch.object(
                probe, "validate_target", return_value=("api.telegram.org", "address", "/path")
            ),
            patch.object(probe, "PinnedHTTPSConnection", return_value=connection),
        ):
            with self.assertRaises(ValueError):
                probe.readonly_api(environment()["STATUS_TG_TEST_TOKEN"], "getMe")
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
