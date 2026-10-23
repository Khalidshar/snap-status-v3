import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from snap_bot import actions_preview as actions
from snap_bot import send_reconciliation as rec
from snap_bot.send_evidence import network_started, tracked_transport

SHA = "a" * 64
INTENT = {
    "run_id": "7",
    "video_sha": SHA,
    "content_hash": "b" * 64,
    "state": "EXTERNAL_EFFECT_INTENT",
    "actor": "Khalidshar",
}
ART = {
    "id": 9,
    "name": "did-you-know-send-attempt-" + SHA,
    "expired": False,
    "workflow_run": {"id": 7, "head_sha": "c" * 40},
}
RUN = {
    "id": 7,
    "repository": {"full_name": rec.REPOSITORY},
    "event": "workflow_dispatch",
    "head_sha": "c" * 40,
    "path": ".github/workflows/did-you-know-test-send.yml",
}
ENTRY = {
    "run_id": "7",
    "video_sha": SHA,
    "intent_artifact_id": "9",
    "intent_sha256": hashlib.sha256(
        json.dumps(INTENT, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest(),
    "reason": "empty TG_TEST credentials; preflight stopped before network",
    "log_url": "https://github.com/"
    + rec.REPOSITORY
    + "/actions/runs/7/job/111550278182#step:10:1",
}
RECEIPT = {
    "run_id": "7",
    "video_sha": SHA,
    "intent_artifact_id": "9",
    "content_hash": "b" * 64,
    "state": "NOT_SENT_PRE_NETWORK",
}


class ReconciliationTests(unittest.TestCase):
    def tearDown(self):
        network_started.set(False)

    def test_unknown_and_missing_stay_blocked(self):
        for state in ("UNKNOWN", "PUBLISHED", "DELIVERY_UNKNOWN", None):
            with self.subTest(state=state):
                self.assertFalse(
                    rec.receipt_allows({**RECEIPT, "state": state}, ART, INTENT),
                    "guard:unknown-blocked",
                )

    def test_pre_network_requires_all_bindings(self):
        self.assertTrue(rec.receipt_allows(RECEIPT, ART, INTENT), "guard:pre-network-allowed")
        for key, value in [
            ("run_id", "8"),
            ("video_sha", "c" * 64),
            ("intent_artifact_id", "10"),
            ("content_hash", "c" * 64),
        ]:
            self.assertFalse(
                rec.receipt_allows({**RECEIPT, key: value}, ART, INTENT), "guard:receipt-binding"
            )

    def test_reviewed_run_and_sha_must_match_api_and_intent(self):
        self.assertTrue(rec.reviewed_allows(ENTRY, ART, INTENT, RUN))
        for key, value in [
            ("run_id", "8"),
            ("video_sha", "c" * 64),
            ("intent_artifact_id", "10"),
            ("intent_sha256", "c" * 64),
        ]:
            self.assertFalse(
                rec.reviewed_allows({**ENTRY, key: value}, ART, INTENT, RUN),
                "guard:reviewed-binding",
            )
        self.assertFalse(
            rec.reviewed_allows(ENTRY, ART, INTENT, {**RUN, "id": 8}), "guard:api-run-binding"
        )
        self.assertFalse(
            rec.bound_intent(ART, {**INTENT, "video_sha": "c" * 64}, SHA),
            "guard:intent-sha-binding",
        )

    def test_every_prior_intent_checked(self):
        with (
            patch("snap_bot.send_reconciliation.load_entries", return_value=[]),
            patch(
                "snap_bot.send_reconciliation.resolve_prior", side_effect=[True, False]
            ) as resolve,
        ):
            with self.assertRaises(ValueError, msg="guard:all-history"):
                actions.history_guard([ART, ART], SHA, {"GITHUB_TOKEN": "synthetic"})
            self.assertEqual(resolve.call_count, 2)

    def test_history_uses_verified_not_sent_receipt(self):
        with (
            patch("snap_bot.send_reconciliation.load_entries", return_value=[]),
            patch("snap_bot.send_reconciliation.resolve_prior", return_value=True),
        ):
            actions.history_guard([ART], SHA, {"GITHUB_TOKEN": "synthetic"})

    def test_unknown_receipt_cannot_be_reconciled(self):
        result = {
            "id": 10,
            "name": "did-you-know-send-result-7",
            "expired": False,
            "workflow_run": {"id": 7},
        }
        reader = lambda artifact, env: (  # noqa: E731
            {"intent.json": INTENT}
            if artifact["id"] == 9
            else {"manual-preview/receipt.json": {**RECEIPT, "state": "UNKNOWN"}}
        )
        api = lambda path, env: {"artifacts": [result]} if "artifacts?" in path else RUN  # noqa: E731
        self.assertFalse(
            rec.resolve_prior(ART, {}, api, [ENTRY], reader), "guard:unknown-reconciliation"
        )

    def test_reviewed_entry_checked_through_api(self):
        api = lambda path, env: {"artifacts": []} if "artifacts?" in path else RUN  # noqa: E731
        self.assertTrue(
            rec.resolve_prior(ART, {}, api, [ENTRY], lambda *args: {"intent.json": INTENT})
        )
        bad = lambda path, env: {"artifacts": []} if "artifacts?" in path else {**RUN, "id": 8}  # noqa: E731
        self.assertFalse(
            rec.resolve_prior(ART, {}, bad, [ENTRY], lambda *args: {"intent.json": INTENT}),
            "guard:api-run-binding",
        )

    def test_send_failure_before_and_after_first_request_is_durable(self):
        for started in (False, True):
            with tempfile.TemporaryDirectory() as directory:

                def failed(*args):
                    if started:
                        tracked_transport(
                            lambda *args: (_ for _ in ()).throw(TimeoutError("SECRET"))
                        )("SECRET", "getMe")
                    raise ValueError("SECRET")

                env = {
                    "GITHUB_RUN_ID": "7",
                    "VIDEO_SHA": SHA,
                    "CONTENT_HASH": "b" * 64,
                    "SEND_INTENT_ARTIFACT_ID": "9",
                }
                with patch.object(actions, "_send_bundle", side_effect=failed):
                    with self.assertRaises(Exception):
                        actions.send_bundle(directory, env)
                raw = (Path(directory) / "receipt.json").read_text()
                value = json.loads(raw)
                self.assertEqual(
                    value["state"],
                    "UNKNOWN" if started else "NOT_SENT_PRE_NETWORK",
                    "guard:network-boundary",
                )
                self.assertNotIn("SECRET", raw)

    def test_missing_credentials_deliver_has_durable_pre_network_receipt(self):
        from types import SimpleNamespace

        from snap_bot.telegram_egress import deliver

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"TG_TEST_TOKEN": "", "TG_TEST_CHAT": ""}),
        ):
            # Use a mocked workspace so no real job, approval or transport exists.
            root = Path(directory)
            with (
                patch("studio_core.storage.Storage.workspace", return_value=root),
                patch("snap_bot.telegram_egress.api") as api,
            ):
                with self.assertRaises(ValueError):
                    deliver(SimpleNamespace(data_root=root), "synthetic", SHA)
                api.assert_not_called()
            value = json.loads((root / "output/pre-network-receipt.json").read_text())
            self.assertEqual(value["state"], "NOT_SENT_PRE_NETWORK", "guard:deliver-pre-network")

    def test_preflight_cannot_skip_history(self):
        from test_actions_preview import environment

        env = environment()
        current = {
            "event": "workflow_dispatch",
            "actor": {"login": "Khalidshar"},
            "triggering_actor": {"login": "Khalidshar"},
        }
        with (
            patch.object(actions, "source_guard"),
            patch.object(actions, "github_get", side_effect=[{}, current, {"artifacts": [ART]}]),
            patch("snap_bot.send_reconciliation.load_entries", return_value=[]),
            patch("snap_bot.send_reconciliation.resolve_prior", return_value=False),
        ):
            with self.assertRaises(ValueError, msg="guard:preflight-history"):
                actions.preflight(env)

    def test_previous_unknown_receipt_cannot_be_downgraded(self):
        from snap_bot.send_evidence import failure_receipt

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps({**RECEIPT, "state": "UNKNOWN"}))
            value = failure_receipt(path, run_id="7", video_sha=SHA, reason="SYNTHETIC")
            self.assertEqual(value["state"], "UNKNOWN", "guard:no-downgrade")
            path.write_text(json.dumps({"receipt": {"delivery": "PUBLISHED"}}))
            value = failure_receipt(path, run_id="7", video_sha=SHA, reason="SYNTHETIC")
            self.assertEqual(value["receipt"]["delivery"], "PUBLISHED", "guard:preserve-ack")

    def test_deliver_has_no_untracked_network_with_fake_transport(self):
        import http.client
        import socket
        import urllib.request
        from contextlib import ExitStack

        import test_telegram_delivery as fixtures

        from snap_bot import telegram_egress

        fixture = fixtures.DeliveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        attempted = []
        untracked = []
        transport_flags = []
        error = None

        def blocked_network(name):
            def blocked(*args, **kwargs):
                attempted.append(name)
                if not network_started.get():
                    untracked.append(name)
                # Block even tracked I/O: this test must never open a real connection.
                raise OSError("SYNTHETIC_NETWORK_BLOCKED")

            return blocked

        def fake_transport(*args, **kwargs):
            transport_flags.append(network_started.get())
            return fixture.transport(*args, **kwargs)

        with ExitStack() as guards:
            guards.enter_context(
                patch.object(urllib.request, "urlopen", side_effect=blocked_network("urlopen"))
            )
            guards.enter_context(
                patch.object(
                    http.client.HTTPConnection,
                    "request",
                    side_effect=blocked_network("HTTPConnection.request"),
                )
            )
            guards.enter_context(
                patch.object(
                    socket, "create_connection", side_effect=blocked_network("create_connection")
                )
            )
            # validate_target captures its DNS resolver as a default argument.
            # Stub only validation/DNS so a direct-api mutant reaches the HTTP guard
            # without DNS or a live Telegram connection; the api function stays real.
            guards.enter_context(
                patch.object(
                    telegram_egress,
                    "validate_target",
                    return_value=("api.telegram.org", "192.0.2.1", "/synthetic"),
                )
            )
            try:
                first = telegram_egress.deliver(
                    fixture.config, fixture.job, fixture.digest, fake_transport
                )
                # Covers confirmed_result/reconcile_published and existing outbox too.
                second = telegram_egress.deliver(
                    fixture.config, fixture.job, fixture.digest, fake_transport
                )
            except Exception as caught:
                error = caught
        # Check recorded calls outside deliver: its error handlers may catch errors.
        self.assertEqual(untracked, [], "guard:untracked-network")
        self.assertEqual(attempted, [], "guard:transport-only-network")
        self.assertIsNone(error, "guard:fake-delivery-success")
        self.assertTrue(transport_flags and all(transport_flags), "guard:tracked-fake-transport")
        self.assertEqual((first["delivery"], second["delivery"]), ("PUBLISHED", "PUBLISHED"))
        self.assertEqual(fixture.calls, ["getMe", "getChat", "sendVideo", "getMe", "getChat"])
