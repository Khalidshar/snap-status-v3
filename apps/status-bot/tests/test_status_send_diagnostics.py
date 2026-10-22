import io
import json
import os
import socket
import sqlite3
import ssl
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from unittest.mock import patch

import test_status_actions_preview as fixtures

from status_bot import actions_preview as actions
from status_bot import send_diagnostics as diagnostics
from status_bot import telegram_egress as telegram
from studio_core.jobs import Repository


class WorkflowDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BundleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(patch.stopall)
        self.credentials = {
            "STATUS_TG_TEST_TOKEN": "123456:" + "x" * 35,
            "STATUS_TG_TEST_CHAT": "42",
        }
        patch.dict(os.environ, self.credentials).start()

    def send(self, transport=None, *, actual_api=False, env=None, github=None):
        f = self.fixture
        return actions.send_bundle(
            f.bundle,
            env or f.env,
            github or f.github,
            None if actual_api else transport or f.transport,
        )

    def receipt(self):
        return json.loads((self.fixture.bundle / "receipt.json").read_text())

    def latest(self):
        return json.loads((self.fixture.bundle / "send-diagnostics.json").read_text())

    def fail(self, call):
        with self.assertRaises(Exception) as caught:
            call()
        return caught.exception

    def test_invalid_approval_is_proven_pre_network(self):
        self.fail(lambda: self.send(env={**self.fixture.env, "HUMAN_APPROVAL": ""}))
        value = self.receipt()
        self.assertEqual(value["stage"], "APPROVAL")
        self.assertEqual(value["state"], "NOT_SENT_PRE_NETWORK")
        self.assertFalse(value["network_started"])
        self.assertEqual(self.fixture.calls, [])

    def test_wrong_intent_is_proven_pre_network(self):
        self.fail(lambda: self.send(github=lambda *args: {"id": 99}))
        self.assertEqual(self.receipt()["stage"], "DURABLE_INTENT")
        self.assertEqual(self.receipt()["state"], "NOT_SENT_PRE_NETWORK")
        self.assertEqual(self.fixture.calls, [])

    def test_bundle_tamper_is_proven_pre_network(self):
        (self.fixture.bundle / "weather.mp4").write_bytes(b"changed")
        self.fail(self.send)
        self.assertEqual(self.receipt()["stage"], "BUNDLE_VALIDATION")
        self.assertEqual(self.receipt()["state"], "NOT_SENT_PRE_NETWORK")
        self.assertEqual(self.fixture.calls, [])

    def test_missing_default_api_token_never_connects(self):
        with patch.dict(os.environ, {"STATUS_TG_TEST_TOKEN": ""}):
            self.fail(lambda: self.send(actual_api=True))
        value = self.receipt()
        self.assertEqual(value["stage"], "CREDENTIAL_VALIDATION")
        self.assertEqual(value["error_code"], "INVALID_TEST_TOKEN")
        self.assertEqual(value["state"], "NOT_SENT_PRE_NETWORK")

    def test_missing_or_group_chat_never_connects(self):
        for chat in ("", "-42"):
            with self.subTest(chat=chat), patch.dict(os.environ, {"STATUS_TG_TEST_CHAT": chat}):
                self.fail(self.send)
                self.assertEqual(self.latest()["error_code"], "INVALID_PRIVATE_CHAT_ID")
                self.assertEqual(self.latest()["state"], "NOT_SENT_PRE_NETWORK")
        self.assertEqual(self.fixture.calls, [])

    def test_get_me_timeout_is_unknown_and_never_logs_exception_text(self):
        secret = self.credentials["STATUS_TG_TEST_TOKEN"]

        def transport(*args):
            raise TimeoutError("https://api.telegram.org/bot" + secret + "/getMe")

        error = self.fail(lambda: self.send(transport))
        value = self.receipt()
        self.assertEqual(value["stage"], "TELEGRAM_GET_ME")
        self.assertEqual(value["state"], "UNKNOWN")
        self.assertEqual(value["error_code"], "TIMEOUT")
        self.assertTrue(value["network_started"])
        self.assertFalse(value["send_video_started"])
        for evidence in (value, self.latest(), error.status_diagnostics):
            text = json.dumps(evidence)
            self.assertNotIn(secret, text)
            self.assertNotIn("api.telegram.org", text)

    def test_wrong_bot_is_unknown_without_send_video(self):
        def transport(*args):
            self.fixture.calls.append(args[1])
            return {"username": "different_bot"}

        self.fail(lambda: self.send(transport))
        self.assertEqual(self.receipt()["stage"], "BOT_IDENTITY")
        self.assertEqual(self.receipt()["error_code"], "BOT_IDENTITY_MISMATCH")
        self.assertEqual(self.receipt()["state"], "UNKNOWN")
        self.assertEqual(self.fixture.calls, ["getMe"])

    def test_get_chat_rejection_records_only_numeric_codes(self):
        def transport(*args):
            if args[1] == "getChat":
                raise diagnostics.TelegramRejected(403, 403)
            return self.fixture.transport(*args)

        self.fail(lambda: self.send(transport))
        value = self.receipt()
        self.assertEqual(value["stage"], "TELEGRAM_GET_CHAT")
        self.assertEqual(value["error_code"], "TELEGRAM_API_REJECTED")
        self.assertEqual(value["http_status"], 403)
        self.assertEqual(value["telegram_error_code"], 403)
        self.assertFalse(value["send_video_started"])

    def test_real_api_parser_does_not_copy_provider_description(self):
        secret = self.credentials["STATUS_TG_TEST_TOKEN"]

        class Response:
            status = 401

            def read(self, limit):
                return json.dumps(
                    {
                        "ok": False,
                        "error_code": 401,
                        "description": secret + " https://secret.example/",
                    }
                ).encode()

        class Connection:
            def __init__(self, *args):
                pass

            def request(self, *args):
                pass

            def getresponse(self):
                return Response()

            def close(self):
                pass

        with (
            patch.object(
                telegram,
                "validate_target",
                return_value=("api.telegram.org", "203.0.113.10", "/fixture"),
            ),
            patch.object(telegram, "PinnedHTTPSConnection", Connection),
        ):
            self.fail(lambda: self.send(actual_api=True))
        value = self.receipt()
        self.assertEqual(value["stage"], "TELEGRAM_GET_ME")
        self.assertEqual(value["http_status"], 401)
        self.assertEqual(value["telegram_error_code"], 401)
        self.assertNotIn(secret, json.dumps(value))
        self.assertNotIn("secret.example", json.dumps(value))

    def test_private_owner_guard_does_not_suggest_group_delivery(self):
        def transport(*args):
            if args[1] == "getChat":
                return {"id": 42, "type": "group"}
            return self.fixture.transport(*args)

        self.fail(lambda: self.send(transport))
        value = self.receipt()
        self.assertEqual(value["stage"], "PRIVATE_OWNER")
        self.assertEqual(value["error_code"], "UNVERIFIED_PRIVATE_OWNER")
        self.assertFalse(value["send_video_started"])
        self.assertNotIn("sendVideo", self.fixture.calls)

    def test_bot_identity_as_destination_never_looks_up_chat_or_sends(self):
        def transport(token, method, *args):
            self.fixture.calls.append(method)
            return {"id": 42, "username": telegram.EXPECTED_BOT, "is_bot": True}

        self.fail(lambda: self.send(transport))
        self.assertEqual(self.receipt()["error_code"], "BOT_ID_USED_AS_CHAT")
        self.assertEqual(self.receipt()["stage"], "PRIVATE_OWNER")
        self.assertFalse(self.receipt()["send_video_started"])
        self.assertFalse(self.receipt()["durable_ack_confirmed"])
        self.assertEqual(self.fixture.calls, ["getMe"])

    def test_explicit_bot_chat_never_reserves_outbox_or_sends(self):
        for chat in [
            {"id": 42, "type": "private", "username": telegram.EXPECTED_BOT},
            {"id": 42, "type": "private", "is_bot": True},
        ]:
            with self.subTest(chat=chat):
                self.fixture.calls.clear()

                def transport(token, method, *args):
                    self.fixture.calls.append(method)
                    return (
                        chat
                        if method == "getChat"
                        else {"id": 123456, "username": telegram.EXPECTED_BOT}
                    )

                self.fail(lambda: self.send(transport))
                self.assertEqual(self.latest()["error_code"], "BOT_ID_USED_AS_CHAT")
                self.assertFalse(self.latest()["send_video_started"])
                self.assertNotIn("sendVideo", self.fixture.calls)
                self.assertEqual(self.fixture.calls, ["getMe", "getChat"])

    def test_classified_telegram_failure_preserves_unknown_and_blocks_replay(self):
        def transport(*args):
            if args[1] == "sendVideo":
                self.fixture.calls.append("sendVideo")
                raise diagnostics.TelegramRejected(403, 403, "RECIPIENT_DEACTIVATED")
            return self.fixture.transport(*args)

        self.fail(lambda: self.send(transport))
        value = self.receipt()
        self.assertEqual(value["telegram_rejection"], "RECIPIENT_DEACTIVATED")
        self.assertEqual(value["state"], "UNKNOWN")
        self.assertFalse(value["telegram_ack_validated"])
        before = (self.fixture.bundle / "receipt.json").read_bytes()
        self.fail(self.send)
        self.assertEqual(self.fixture.calls.count("sendVideo"), 1)
        self.assertEqual((self.fixture.bundle / "receipt.json").read_bytes(), before)

    def test_send_timeout_keeps_unknown_and_never_replays(self):
        def transport(*args):
            if args[1] == "sendVideo":
                self.fixture.calls.append("sendVideo")
                raise TimeoutError("sensitive remote URL")
            return self.fixture.transport(*args)

        self.fail(lambda: self.send(transport))
        value = self.receipt()
        self.assertEqual(value["stage"], "TELEGRAM_SEND_VIDEO")
        self.assertEqual(value["error_code"], "TIMEOUT")
        self.assertEqual(value["state"], "UNKNOWN")
        self.assertTrue(value["send_video_started"])
        before = (self.fixture.bundle / "receipt.json").read_bytes()
        self.fail(self.send)
        self.assertEqual(self.fixture.calls.count("sendVideo"), 1)
        self.assertEqual((self.fixture.bundle / "receipt.json").read_bytes(), before)
        self.assertEqual(self.latest()["state"], "UNKNOWN")
        self.assertTrue(self.latest()["prior_unknown"])
        self.assertEqual(self.latest()["stage"], "BUNDLE_VALIDATION")

    def test_invalid_ack_is_unknown_and_not_published(self):
        def transport(*args):
            if args[1] == "sendVideo":
                self.fixture.calls.append("sendVideo")
                return {"message_id": 77, "chat": {"id": 43}}
            return self.fixture.transport(*args)

        self.fail(lambda: self.send(transport))
        value = self.receipt()
        self.assertEqual(value["stage"], "ACK_VALIDATION")
        self.assertEqual(value["error_code"], "UNVERIFIED_TELEGRAM_ACK")
        self.assertEqual(value["state"], "UNKNOWN")
        self.assertFalse(value["durable_ack_confirmed"])

    def test_outbox_reservation_failure_is_unknown_without_video_call(self):
        with patch.object(
            Repository, "transaction", side_effect=sqlite3.OperationalError("sensitive SQL")
        ):
            self.fail(self.send)
        value = self.receipt()
        self.assertEqual(value["stage"], "OUTBOX_RESERVATION")
        self.assertEqual(value["error_code"], "DATABASE_ERROR")
        self.assertEqual(value["state"], "UNKNOWN")
        self.assertFalse(value["send_video_started"])
        self.assertEqual(self.fixture.calls, ["getMe", "getChat"])

    def test_ack_commit_failure_does_not_claim_durable_publication(self):
        original = Repository.transaction
        failed = []

        @contextmanager
        def transaction(repository):
            with original(repository) as db:
                yield db
                current = diagnostics._current.get()
                if current is not None and current.stage == "ACK_PERSISTENCE" and not failed:
                    failed.append(True)
                    raise sqlite3.OperationalError("sensitive commit error")

        with patch.object(Repository, "transaction", new=transaction):
            self.fail(self.send)
        value = self.receipt()
        self.assertEqual(value["stage"], "ACK_PERSISTENCE")
        self.assertEqual(value["error_code"], "DATABASE_ERROR")
        self.assertEqual(value["state"], "UNKNOWN")
        self.assertTrue(value["telegram_ack_validated"])
        self.assertFalse(value["durable_ack_confirmed"])
        self.assertEqual(self.fixture.calls.count("sendVideo"), 1)
        with Repository(actions.bundle_config(self.fixture.bundle)).connect() as db:
            self.assertEqual(
                db.execute("SELECT state FROM telegram_outbox").fetchone()[0], "DELIVERY_UNKNOWN"
            )

    def test_post_ack_manifest_failure_does_not_downgrade_publication(self):
        with patch.object(telegram.Storage, "manifest", side_effect=OSError("sensitive path")):
            self.fail(self.send)
        value = self.receipt()
        self.assertEqual(value["stage"], "LOCAL_FINALIZATION")
        self.assertEqual(value["state"], "ACK_CONFIRMED_LOCAL_EVIDENCE_PENDING")
        self.assertEqual(value["delivery"], "PUBLISHED")
        self.assertTrue(value["durable_ack_confirmed"])
        self.assertEqual(value["message_id"], 77)
        with Repository(actions.bundle_config(self.fixture.bundle)).connect() as db:
            self.assertEqual(
                db.execute("SELECT state FROM telegram_outbox").fetchone()[0], "PUBLISHED"
            )
        self.fail(self.send)
        self.assertEqual(self.fixture.calls.count("sendVideo"), 1)
        self.assertEqual(self.receipt()["delivery"], "PUBLISHED")

    def test_success_receipt_bytes_are_preserved_after_rejected_reexecution(self):
        self.send()
        before = (self.fixture.bundle / "receipt.json").read_bytes()
        self.fail(self.send)
        self.assertEqual((self.fixture.bundle / "receipt.json").read_bytes(), before)
        self.assertEqual(self.receipt()["receipt"]["delivery"], "PUBLISHED")
        self.assertEqual(self.latest()["stage"], "BUNDLE_VALIDATION")
        self.assertTrue(self.latest()["durable_ack_confirmed"])
        self.assertEqual(self.fixture.calls.count("sendVideo"), 1)

    def test_success_receipt_write_failure_keeps_durable_ack(self):
        original = telegram.atomic_json

        def writer(path, value):
            if Path(path) == self.fixture.bundle / "receipt.json" and "human_review" in value:
                raise OSError("sensitive disk error")
            return original(path, value)

        with patch.object(telegram, "atomic_json", side_effect=writer):
            self.fail(self.send)
        value = self.receipt()
        self.assertEqual(value["stage"], "RECEIPT_PERSISTENCE")
        self.assertEqual(value["delivery"], "PUBLISHED")
        self.assertTrue(value["durable_ack_confirmed"])
        self.assertEqual(self.fixture.calls.count("sendVideo"), 1)

    def test_diagnostic_write_failure_does_not_mask_original_exception(self):
        with patch.object(diagnostics, "_atomic", side_effect=OSError("sensitive evidence error")):
            error = self.fail(lambda: self.send(env={**self.fixture.env, "HUMAN_APPROVAL": ""}))
        self.assertIsInstance(error, ValueError)
        self.assertTrue(error.status_diagnostics["evidence_write_failed"])
        self.assertEqual(error.status_diagnostics["stage"], "APPROVAL")
        self.assertEqual(self.fixture.calls, [])

    def test_cli_prints_safe_diagnostic_when_artifact_cannot_be_written(self):
        f = self.fixture
        env = {
            **f.env,
            "HUMAN_APPROVAL": "",
            "STATUS_TG_TEST_TOKEN": self.credentials["STATUS_TG_TEST_TOKEN"],
        }
        err = io.StringIO()
        with (
            patch.dict(os.environ, env),
            patch("sys.argv", ["status-send", "send", "--bundle", str(f.bundle)]),
            patch.object(diagnostics, "_atomic", side_effect=OSError("secret.example")),
            redirect_stderr(err),
            self.assertRaises(SystemExit),
        ):
            actions.main()
        text = err.getvalue()
        value = json.loads(text.splitlines()[0])
        self.assertEqual(value["stage"], "APPROVAL")
        self.assertTrue(value["evidence_write_failed"])
        self.assertNotIn("secret.example", text)
        self.assertNotIn(self.credentials["STATUS_TG_TEST_TOKEN"], text)

    def test_cli_copies_diagnostic_to_existing_artifact_without_changing_intent(self):
        f = self.fixture
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                actions.write_intent(f.env)
                intent_path = Path("outputs/send-intent/intent.json")
                before = intent_path.read_bytes()
                err = io.StringIO()
                with (
                    patch.dict(os.environ, {**f.env, "HUMAN_APPROVAL": ""}),
                    patch("sys.argv", ["status-send", "send", "--bundle", str(f.bundle)]),
                    redirect_stderr(err),
                    self.assertRaises(SystemExit),
                ):
                    actions.main()
                copied = json.loads(Path("outputs/send-intent/send-diagnostics.json").read_text())
                self.assertEqual(copied["stage"], "APPROVAL")
                self.assertEqual(copied["state"], "NOT_SENT_PRE_NETWORK")
                self.assertEqual(intent_path.read_bytes(), before)
                self.assertEqual(self.fixture.calls, [])
            finally:
                os.chdir(old_cwd)

    def test_cli_does_not_copy_diagnostic_into_unrelated_intent(self):
        f = self.fixture
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                actions.write_intent({**f.env, "VIDEO_SHA": "0" * 64})
                intent_path = Path("outputs/send-intent/intent.json")
                before = intent_path.read_bytes()
                with (
                    patch.dict(os.environ, {**f.env, "HUMAN_APPROVAL": ""}),
                    patch("sys.argv", ["status-send", "send", "--bundle", str(f.bundle)]),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    actions.main()
                self.assertFalse(Path("outputs/send-intent/send-diagnostics.json").exists())
                self.assertEqual(intent_path.read_bytes(), before)
            finally:
                os.chdir(old_cwd)

    def test_cli_never_reads_untrusted_exception_diagnostic_attributes(self):
        secret = self.credentials["STATUS_TG_TEST_TOKEN"]
        reads = []

        class ExternalError(RuntimeError):
            @property
            def status_diagnostics(self):
                reads.append(True)
                return {"leak": secret + " https://secret.example/"}

        err = io.StringIO()
        with (
            patch.dict(os.environ, self.fixture.env),
            patch("sys.argv", ["status-send", "send", "--bundle", str(self.fixture.bundle)]),
            patch.object(actions, "send_bundle", side_effect=ExternalError()),
            redirect_stderr(err),
            self.assertRaises(SystemExit),
        ):
            actions.main()
        self.assertEqual(reads, [])
        self.assertNotIn(secret, err.getvalue())
        self.assertNotIn("secret.example", err.getvalue())
        self.assertEqual(json.loads(err.getvalue().splitlines()[0])["error_code"], "INTERNAL_ERROR")

    def test_existing_intent_guard_remains_unconditional(self):
        for expired in (False, True):
            with self.subTest(expired=expired), self.assertRaises(ValueError):
                actions.history_guard(
                    [
                        {
                            "name": actions.INTENT_PREFIX + self.fixture.env["VIDEO_SHA"],
                            "expired": expired,
                        }
                    ],
                    self.fixture.env["VIDEO_SHA"],
                )
        # The lab's GitHub Actions workflow file is not part of v3 (GRADUATION.md).


class DiagnosticEvidenceTests(unittest.TestCase):
    def test_legacy_unknown_or_corrupt_receipt_is_not_overwritten(self):
        for raw in (
            json.dumps({"delivery": "SEND_REJECTED_OR_UNKNOWN"}),
            "{broken",
            json.dumps({"receipt": "not-a-proof"}),
        ):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "receipt.json"
                path.write_text(raw)
                with diagnostics.operation():
                    diagnostics.stage("APPROVAL")
                    diagnostics.failure_receipt(path, fixtures.environment())
                self.assertEqual(path.read_text(), raw)
                latest = json.loads(path.with_name("send-diagnostics.json").read_text())
                self.assertEqual(latest["state"], "UNKNOWN")

    def test_prior_published_receipt_for_other_sha_is_kept_without_claiming_current_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            raw = json.dumps(
                {"receipt": {"delivery": "PUBLISHED", "video_sha256": "0" * 64, "message_id": 77}}
            )
            path.write_text(raw)
            with diagnostics.operation():
                diagnostics.failure_receipt(path, fixtures.environment())
            self.assertEqual(path.read_text(), raw)
            latest = json.loads(path.with_name("send-diagnostics.json").read_text())
            self.assertFalse(latest["durable_ack_confirmed"])
            self.assertEqual(latest["state"], "UNKNOWN")

    def test_untrusted_identifier_fields_and_exception_messages_are_not_copied(self):
        env = {
            key: "https://secret.example/token"
            for key in ("GITHUB_RUN_ID", "VIDEO_SHA", "CONTENT_HASH", "SEND_INTENT_ARTIFACT_ID")
        }
        with diagnostics.operation():
            diagnostics.record_error(RuntimeError("secret exception"))
            value = diagnostics.payload(env)
        for key in ("run_id", "video_sha", "content_hash", "intent_id"):
            self.assertIsNone(value[key])
        self.assertNotIn("secret", json.dumps(value))

    def test_missing_or_boolean_message_id_never_establishes_durable_ack(self):
        for message_id in (None, True, False, 0, -1, "77"):
            with self.subTest(message_id=message_id), diagnostics.operation():
                diagnostics.acknowledge(message_id, durable=True)
                self.assertFalse(diagnostics.payload({})["durable_ack_confirmed"])

    def test_published_label_without_valid_message_id_is_preserved_but_not_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            raw = json.dumps({"receipt": {"delivery": "PUBLISHED", "video_sha256": "a" * 64}})
            path.write_text(raw)
            with diagnostics.operation():
                diagnostics.failure_receipt(path, fixtures.environment())
            self.assertEqual(path.read_text(), raw)
            latest = json.loads(path.with_name("send-diagnostics.json").read_text())
            self.assertFalse(latest["durable_ack_confirmed"])
            self.assertEqual(latest["state"], "UNKNOWN")

    def test_classification_uses_only_fixed_codes(self):
        cases = (
            (socket.gaierror("secret DNS"), "DNS_ERROR"),
            (ssl.SSLError("secret TLS"), "TLS_ERROR"),
            (ConnectionError("secret host"), "CONNECTION_ERROR"),
            (sqlite3.OperationalError("secret query"), "DATABASE_ERROR"),
        )
        for error, code in cases:
            with self.subTest(code=code), diagnostics.operation():
                diagnostics.record_error(error)
                self.assertEqual(diagnostics.payload({})["error_code"], code)

    def test_default_payload_without_context_is_conservative(self):
        self.assertEqual(diagnostics.payload({})["state"], "UNKNOWN")

    def test_concurrent_operations_do_not_share_network_or_ack_evidence(self):
        ready = threading.Barrier(2)

        def task(effect):
            with diagnostics.operation():
                ready.wait(timeout=5)
                if effect:
                    diagnostics.telegram_call(lambda *args: {}, "", "sendVideo")
                    diagnostics.acknowledge(77, durable=True)
                else:
                    diagnostics.stage("APPROVAL")
                ready.wait(timeout=5)
                return diagnostics.payload({})

        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(task, (False, True)))
        self.assertEqual(values[0]["state"], "NOT_SENT_PRE_NETWORK")
        self.assertEqual(values[1]["delivery"], "PUBLISHED")
        self.assertFalse(values[0]["send_video_started"])
        self.assertTrue(values[1]["send_video_started"])
        self.assertIsNone(diagnostics._current.get())

    def test_atomic_failure_preserves_existing_receipt_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text('{"original":true}')
            with patch.object(telegram.os, "replace", side_effect=OSError("disk fault")):
                with self.assertRaises(OSError):
                    telegram.atomic_json(path, {"replacement": True})
            self.assertEqual(path.read_text(), '{"original":true}')
            self.assertEqual([p.name for p in path.parent.iterdir()], ["receipt.json"])


if __name__ == "__main__":
    unittest.main()
