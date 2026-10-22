import json
import unittest
from unittest.mock import Mock, patch

from status_bot import send_diagnostics as diagnostics
from status_bot import telegram_egress as telegram
from status_bot.telegram_errors import REJECTION_DESCRIPTIONS, UNKNOWN_REJECTION, classify_rejection


class RejectionClassificationTests(unittest.TestCase):
    def setUp(self):
        policy = patch("status_bot.platform_policy.reject_status_telegram", new=lambda: None)
        policy.start()
        self.addCleanup(policy.stop)

    def test_every_exact_mapping_requires_matching_codes(self):
        for description, (status, code) in REJECTION_DESCRIPTIONS.items():
            with self.subTest(code=code):
                self.assertEqual(classify_rejection(status, status, description), code)
                for http, api in [(400, 403), (None, status), (status, True)]:
                    self.assertEqual(classify_rejection(http, api, description), UNKNOWN_REJECTION)

    def test_arbitrary_descriptions_are_never_classified_by_substring(self):
        secret = "123456:" + "x" * 35
        for value in [
            secret,
            {"description": secret},
            [secret],
            None,
            "x" * 1000,
            "Forbidden: bot was blocked by the user " + secret,
        ]:
            with self.subTest(value_type=type(value).__name__):
                self.assertEqual(classify_rejection(403, 403, value), UNKNOWN_REJECTION)

    def test_classification_is_retained_without_changing_generic_error_or_retry(self):
        with diagnostics.operation():
            with self.assertRaises(diagnostics.TelegramRejected):
                diagnostics.telegram_call(
                    Mock(side_effect=diagnostics.TelegramRejected(403, 403, "BOT_BLOCKED_BY_USER")),
                    "",
                    "sendVideo",
                )
            value = diagnostics.payload({})
            self.assertEqual(value["telegram_rejection"], "BOT_BLOCKED_BY_USER")
            self.assertEqual(value["error_code"], "TELEGRAM_API_REJECTED")
            self.assertEqual(value["state"], "UNKNOWN")
            self.assertEqual(value["retry"], "NEVER_AUTOMATIC")

    def test_api_parser_preserves_only_fixed_safe_classification(self):
        for description, (status, code) in REJECTION_DESCRIPTIONS.items():
            response = Mock(status=status)
            response.read.return_value = json.dumps(
                {"ok": False, "error_code": status, "description": description}
            ).encode()
            connection = Mock()
            connection.getresponse.return_value = response
            with (
                self.subTest(code=code),
                patch.object(
                    telegram,
                    "validate_target",
                    return_value=("api.telegram.org", "203.0.113.10", "/fixture"),
                ),
                patch.object(telegram, "PinnedHTTPSConnection", return_value=connection),
                diagnostics.operation(),
            ):
                with self.assertRaises(diagnostics.TelegramRejected) as caught:
                    diagnostics.telegram_call(telegram.api, "", "sendVideo")
                value = diagnostics.payload({})
                self.assertEqual(value["telegram_rejection"], code)
                self.assertEqual(value["error_code"], "TELEGRAM_API_REJECTED")
                self.assertEqual(value["state"], "UNKNOWN")
                self.assertEqual(value["retry"], "NEVER_AUTOMATIC")
                self.assertFalse(value["durable_ack_confirmed"])
                self.assertNotIn(description, str(caught.exception))
                self.assertNotIn("description", caught.exception.__dict__)
                connection.close.assert_called_once()

    def test_unrecognized_api_description_and_mutated_classification_are_redacted(self):
        secret = "123456:" + "x" * 35
        response = Mock(status=403)
        response.read.return_value = json.dumps(
            {"ok": False, "error_code": 403, "description": secret}
        ).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        with (
            patch.object(
                telegram,
                "validate_target",
                return_value=("api.telegram.org", "203.0.113.10", "/fixture"),
            ),
            patch.object(telegram, "PinnedHTTPSConnection", return_value=connection),
            diagnostics.operation(),
        ):
            with self.assertRaises(diagnostics.TelegramRejected) as caught:
                diagnostics.telegram_call(telegram.api, "", "sendVideo")
            value = diagnostics.payload({})
            self.assertEqual(value["telegram_rejection"], UNKNOWN_REJECTION)
            self.assertNotIn(secret, json.dumps(value))
            self.assertNotIn(secret, str(caught.exception))
            self.assertNotIn(secret, json.dumps(caught.exception.__dict__))
        for untrusted in [secret, [secret], {"value": secret}]:
            with diagnostics.operation() as diagnostic:
                diagnostic.telegram_rejection = untrusted
                self.assertEqual(diagnostics.payload({})["telegram_rejection"], UNKNOWN_REJECTION)
