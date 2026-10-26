"""Offline contracts only. All source/video/API/webhook bytes are unit fixtures."""

import copy
import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_status_weather import TINY_MP4, FixtureClient

from status_bot.pipeline import prepare, record_validation
from status_bot.whatsapp_contract import (
    ApiResponse,
    DeliveryBlocked,
    DeliveryIdentity,
    json_bytes,
    parse_json,
    response_id,
    sha,
    sign_review,
)
from status_bot.whatsapp_egress import DeliveryWorker, GraphCredentials, GraphTransport, graph_post
from status_bot.whatsapp_outbox import WhatsAppOutbox, read_bound
from studio_core.config import Config
from studio_core.jobs import LeaseLost, Repository
from studio_core.jobs.component_lock import ComponentBusy, ComponentLocks
from studio_core.storage import Storage


class OfflineTransport:
    def __init__(self, identity):
        self.credentials = SimpleNamespace(
            phone_number=identity.phone_number, recipient=identity.recipient
        )
        self.upload_video = Mock(return_value=ApiResponse(200, b'{"id":"123456"}'))
        self.send_video = Mock(
            return_value=ApiResponse(
                200,
                json_bytes(
                    {
                        "messaging_product": "whatsapp",
                        "contacts": [{"wa_id": identity.recipient}],
                        "messages": [{"id": "wamid.UNIT_FIXTURE_ACK"}],
                    }
                ),
            )
        )


class WhatsAppContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = replace(
            Config.load(Path(__file__).parents[3] / "config/status/weather.toml"),
            data_root=self.root / "data",
        )
        # Deliberately fictitious identities and keys; never production credentials.
        self.identity = DeliveryIdentity(
            "12345001",
            "12345002",
            "10000000000",
            b"unit-fixture-review-key-32-bytes-only",
            b"unit-fixture-webhook-key-32-bytes-only",
        )
        self.settings = {
            "city": "الرياض",
            "ttl_seconds": 3600,
            "sources": [
                {
                    "id": "open-meteo",
                    "url": "https://api.open-meteo.com/fixture",
                    "license": "unit-fixture-license",
                    "attribution": "Unit fixture only",
                }
            ],
        }
        self.repo = Repository(self.cfg)
        self.outbox = WhatsAppOutbox(self.repo, self.identity)
        self.transport = OfflineTransport(self.identity)

    def fixture(self, event="wa_fixture"):
        value = prepare(
            self.cfg,
            self.settings,
            FixtureClient(),
            "unit-fixture-gateway-key-32-bytes-only",
            event,
        )
        claim = value["claim"]
        job = claim["id"]
        root = Storage(self.cfg).workspace(job)
        video = TINY_MP4
        (root / "output/weather.mp4").write_bytes(video)
        result = {
            "job_id": job,
            "content_hash": value["content"]["content_hash"],
            "video_sha256": hashlib.sha256(video).hexdigest(),
            "glyph_coverage": "PASS",
            "network_isolation": {k: True for k in ("ipv4", "ipv6", "udp", "egress_unreachable")},
            "qa": {
                "result": "PASS",
                "width": 1080,
                "height": 1920,
                "codec": "h264",
                "bytes": len(video),
                "audio": "not_required_for_weather_card",
                "visual_review": "PENDING",
            },
        }
        (root / "output/render-result.json").write_text(json.dumps(result))
        manifest = record_validation(self.cfg, value, result)
        now = time.time()
        review = {
            "app": self.cfg.app,
            "job_id": job,
            "attempt": claim["attempt"],
            "content_hash": result["content_hash"],
            "video_sha256": result["video_sha256"],
            "manifest_sha256": sha(manifest),
            "identity_binding": self.identity.binding,
            "decision": "APPROVE_WHATSAPP_VIDEO",
            "issued_at": now,
            "expires_at": now + 120,
            "nonce": "a" * 32,
        }
        signature = sign_review(review, self.identity.gate_key)
        return value, root, manifest, review, signature

    def queued(self):
        value, root, _manifest, review, signature = self.fixture()
        claim = value["claim"]
        receipt = self.outbox.prepare(
            claim["id"], claim["lease_token"], claim["attempt"], review, signature
        )
        return claim, root, review, signature, receipt["id"]

    def webhook(
        self,
        *,
        status=None,
        message_id="wamid.UNIT_FIXTURE_ACK",
        timestamp=None,
        recipient=None,
        inbound=False,
        phone=None,
        business=None,
    ):
        at = str(int(time.time()) if timestamp is None else timestamp)
        payload = {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": phone or self.identity.phone_number},
        }
        if inbound:
            payload["messages"] = [
                {
                    "from": recipient or self.identity.recipient,
                    "id": message_id,
                    "timestamp": at,
                    "type": "text",
                    "text": {"body": "unit fixture"},
                }
            ]
        if status:
            payload["statuses"] = [
                {
                    "recipient_id": recipient or self.identity.recipient,
                    "id": message_id,
                    "timestamp": at,
                    "status": status,
                }
            ]
        value = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": business or self.identity.business_account,
                    "changes": [{"field": "messages", "value": payload}],
                }
            ],
        }
        raw = json_bytes(value)
        signature = "sha256=" + hmac.new(self.identity.webhook_key, raw, hashlib.sha256).hexdigest()
        return raw, signature

    def open_window(self):
        return self.outbox.webhook(*self.webhook(inbound=True))

    def worker(self):
        return DeliveryWorker(self.outbox, self.transport, enabled=True)

    def test_prepared_does_not_send_and_keeps_manual_publication_distinct(self):
        claim, _root, _review, _sig, item = self.queued()
        receipt = self.outbox.receipt(item)
        self.assertEqual(receipt["state"], "PREPARED")
        self.assertFalse(receipt["recipient_delivery_confirmed"])
        self.assertFalse(receipt["automatic_retry"])
        self.assertEqual(receipt["publication"], "MANUAL_PUBLICATION_PENDING")
        self.assertEqual(self.repo.get(claim["id"])["state"], "VALIDATING")
        self.transport.upload_video.assert_not_called()
        self.transport.send_video.assert_not_called()

    def test_live_transport_and_worker_are_disabled_by_default(self):
        with self.assertRaisesRegex(DeliveryBlocked, "WA_LIVE_TRANSPORT_DISABLED"):
            GraphTransport(None)
        with self.assertRaisesRegex(DeliveryBlocked, "WA_DELIVERY_WORKER_DISABLED"):
            DeliveryWorker(self.outbox, self.transport)

    def test_verified_api_ack_is_not_delivery_or_publication(self):
        claim, root, review, sig, item = self.queued()
        self.open_window()
        receipt = self.worker().deliver(item, claim["lease_token"], review, sig)
        self.assertEqual(receipt["state"], "API_ACCEPTED")
        self.assertFalse(receipt["recipient_delivery_confirmed"])
        self.assertEqual(receipt["publication"], "MANUAL_PUBLICATION_PENDING")
        self.transport.upload_video.assert_called_once_with(
            (root / "output/weather.mp4").read_bytes()
        )
        self.transport.send_video.assert_called_once_with("123456")
        self.assertEqual(self.repo.get(claim["id"])["state"], "DELIVERING")

    def test_fake_transport_delivery_cannot_open_untracked_public_network(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()
        with (
            patch(
                "urllib.request.urlopen", side_effect=AssertionError("Unexpected network")
            ) as urlopen,
            patch(
                "http.client.HTTPConnection.request", side_effect=AssertionError("Unexpected HTTP")
            ) as http,
            patch("socket.socket", side_effect=AssertionError("Unexpected socket")) as socket,
        ):
            self.assertEqual(
                self.worker().deliver(item, claim["lease_token"], review, sig)["state"],
                "API_ACCEPTED",
            )
        urlopen.assert_not_called()
        http.assert_not_called()
        socket.assert_not_called()

    def test_verified_delivery_read_and_out_of_order_receipts_do_not_regress(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()
        self.worker().deliver(item, claim["lease_token"], review, sig)
        for status, expected in [
            ("delivered", "DELIVERED"),
            ("sent", "DELIVERED"),
            ("read", "READ"),
            ("failed", "READ"),
            ("delivered", "READ"),
        ]:
            self.outbox.webhook(*self.webhook(status=status))
            receipt = self.outbox.receipt(item)
            self.assertEqual(receipt["state"], expected)
            self.assertTrue(receipt["recipient_delivery_confirmed"])
            self.assertEqual(receipt["publication"], "MANUAL_PUBLICATION_PENDING")
        self.assertEqual(self.repo.get(claim["id"])["state"], "COMPLETED")
        self.assertIsNone(self.repo.get(claim["id"])["artifacts_terminal_at"])
        self.assertEqual(Storage(self.cfg).gc(self.repo), [])

    def test_receipt_before_http_ack_is_buffered_and_applied(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()

        def send(_):
            self.outbox.webhook(*self.webhook(status="delivered"))
            return ApiResponse(
                200,
                json_bytes(
                    {
                        "messaging_product": "whatsapp",
                        "contacts": [{"wa_id": self.identity.recipient}],
                        "messages": [{"id": "wamid.UNIT_FIXTURE_ACK"}],
                    }
                ),
            )

        self.transport.send_video.side_effect = send
        self.assertEqual(
            self.worker().deliver(item, claim["lease_token"], review, sig)["state"], "DELIVERED"
        )

    def test_failure_or_malformed_ack_freezes_without_second_send(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()
        self.transport.send_video.side_effect = TimeoutError("unit token=SECRET recipient=PRIVATE")
        result = self.worker().deliver(item, claim["lease_token"], review, sig)
        self.assertEqual(result["state"], "DELIVERY_UNKNOWN")
        with self.assertRaises(DeliveryBlocked):
            self.worker().deliver(item, claim["lease_token"], review, sig)
        self.transport.send_video.assert_called_once()
        self.assertNotIn("SECRET", json.dumps(result) + json.dumps(self.repo.timeline(claim["id"])))
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_upload_failure_never_reaches_messages(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()
        self.transport.upload_video.return_value = ApiResponse(200, b'{"id":"bad"}')
        receipt = self.worker().deliver(item, claim["lease_token"], review, sig)
        self.assertEqual(receipt["state"], "UPLOAD_UNKNOWN")
        self.transport.send_video.assert_not_called()

    def test_known_api_rejection_records_numeric_code_only_and_no_replay(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()
        self.transport.send_video.return_value = ApiResponse(
            400, b'{"error":{"code":131047,"message":"SECRET"}}'
        )
        result = self.worker().deliver(item, claim["lease_token"], review, sig)
        self.assertEqual(result["state"], "REJECTED")
        self.assertEqual(result["error_code"], "WA_API_REJECTED_131047")
        with self.assertRaises(DeliveryBlocked):
            self.worker().deliver(item, claim["lease_token"], review, sig)
        self.transport.send_video.assert_called_once()

    def test_redirect_server_error_and_invalid_recipient_ack_are_unknown(self):
        for response in (
            ApiResponse(302, b"{}"),
            ApiResponse(500, b'{"error":{"code":1}}'),
            ApiResponse(
                200,
                b'{"messaging_product":"whatsapp","contacts":[{"wa_id":"WRONG"}],"messages":[{"id":"wamid.x"}]}',
            ),
        ):
            with self.subTest(status=response.status):
                with self.assertRaisesRegex(DeliveryBlocked, "WA_RESPONSE_UNKNOWN"):
                    response_id(response, "send", self.identity.recipient)

    def test_duplicate_preparation_and_concurrent_workers_have_one_effect(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()
        with self.assertRaisesRegex(DeliveryBlocked, "WA_DUPLICATE_BLOCKED"):
            self.outbox.prepare(claim["id"], claim["lease_token"], claim["attempt"], review, sig)
        entered = threading.Event()
        release = threading.Event()

        def upload(_):
            entered.set()
            self.assertTrue(release.wait(5))
            return ApiResponse(200, b'{"id":"123456"}')

        self.transport.upload_video.side_effect = upload
        with ThreadPoolExecutor(2) as executor:
            future = executor.submit(self.worker().deliver, item, claim["lease_token"], review, sig)
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises(ComponentBusy):
                    self.worker().deliver(item, claim["lease_token"], review, sig)
            finally:
                release.set()
            self.assertEqual(future.result(5)["state"], "API_ACCEPTED")
        self.transport.upload_video.assert_called_once()
        self.transport.send_video.assert_called_once()

    def test_approval_wrong_key_scope_or_expiry_blocks_before_transport(self):
        claim, _root, review, _sig, item = self.queued()
        self.open_window()
        variants = []
        for key, value in [
            ("app", "snap-bot-v2"),
            ("attempt", 2),
            ("video_sha256", "0" * 64),
            ("identity_binding", "0" * 64),
            ("expires_at", time.time() - 1),
        ]:
            changed = dict(review)
            changed[key] = value
            variants.append(changed)
        for changed in variants:
            with self.assertRaises(DeliveryBlocked):
                self.worker().deliver(
                    item,
                    claim["lease_token"],
                    changed,
                    sign_review(changed, self.identity.gate_key),
                )
        with self.assertRaises(DeliveryBlocked):
            self.worker().deliver(item, claim["lease_token"], review, "0" * 64)
        self.transport.upload_video.assert_not_called()
        self.transport.send_video.assert_not_called()

    def test_missing_or_stale_service_window_blocks_before_upload(self):
        claim, _root, review, sig, item = self.queued()
        for stale in (False, True):
            if stale:
                self.outbox.webhook(*self.webhook(inbound=True, timestamp=int(time.time()) - 86401))
            with self.assertRaisesRegex(DeliveryBlocked, "WA_SERVICE_WINDOW_CLOSED"):
                self.worker().deliver(item, claim["lease_token"], review, sig)
        self.transport.upload_video.assert_not_called()
        self.assertEqual(self.outbox.receipt(item)["state"], "PREPARED")

    def test_window_or_owner_expiry_during_upload_prevents_message(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()

        def upload(_):
            with self.repo.transaction() as db:
                db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (claim["id"],))
            return ApiResponse(200, b'{"id":"123456"}')

        self.transport.upload_video.side_effect = upload
        receipt = self.worker().deliver(item, claim["lease_token"], review, sig)
        self.assertEqual(receipt["state"], "UPLOAD_UNKNOWN")
        self.transport.send_video.assert_not_called()

    def test_stale_attempt_blocks_before_transport(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()
        with self.repo.transaction() as db:
            db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (claim["id"],))
        next_claim = self.repo.claim(claim["id"])
        self.assertEqual(next_claim["attempt"], 2)
        with self.assertRaises(LeaseLost):
            self.worker().deliver(item, claim["lease_token"], review, sig)
        self.transport.upload_video.assert_not_called()
        self.transport.send_video.assert_not_called()

    def test_cancelled_job_cannot_upload_or_send(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()
        self.repo.transition(claim["id"], claim["lease_token"], "CANCELLED")
        with self.assertRaises(LeaseLost):
            self.worker().deliver(item, claim["lease_token"], review, sig)
        self.transport.upload_video.assert_not_called()
        self.transport.send_video.assert_not_called()

    def test_window_closing_during_upload_prevents_messages_post(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()

        def upload(_):
            with self.repo.transaction() as db:
                db.execute("UPDATE whatsapp_windows SET last_inbound_at=?", (time.time() - 86401,))
            return ApiResponse(200, b'{"id":"123456"}')

        self.transport.upload_video.side_effect = upload
        self.assertEqual(
            self.worker().deliver(item, claim["lease_token"], review, sig)["state"],
            "UPLOAD_UNKNOWN",
        )
        self.transport.send_video.assert_not_called()

    def test_review_expiring_during_upload_prevents_messages_post(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()

        def upload(_):
            self.repo.clock = lambda: review["expires_at"] + 1
            return ApiResponse(200, b'{"id":"123456"}')

        self.transport.upload_video.side_effect = upload
        self.assertEqual(
            self.worker().deliver(item, claim["lease_token"], review, sig)["state"],
            "UPLOAD_UNKNOWN",
        )
        self.transport.send_video.assert_not_called()

    def test_malformed_send_ack_is_unknown_and_signed_unlinked_receipt_cannot_resend(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()
        self.transport.send_video.return_value = ApiResponse(200, b'{"messages":[]}')
        self.assertEqual(
            self.worker().deliver(item, claim["lease_token"], review, sig)["state"],
            "DELIVERY_UNKNOWN",
        )
        self.outbox.webhook(*self.webhook(status="delivered"))
        self.assertEqual(self.outbox.receipt(item)["state"], "DELIVERY_UNKNOWN")
        with self.assertRaises(DeliveryBlocked):
            self.worker().deliver(item, claim["lease_token"], review, sig)
        self.transport.send_video.assert_called_once()

    def test_confirmed_webhook_can_finish_late_unknown_job_without_new_send(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()
        self.worker().deliver(item, claim["lease_token"], review, sig)
        with self.repo.transaction() as db:
            db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (claim["id"],))
        self.assertIsNone(self.repo.claim(claim["id"]))
        self.assertEqual(self.repo.get(claim["id"])["error_code"], "DELIVERY_UNKNOWN")
        self.outbox.webhook(*self.webhook(status="delivered"))
        self.assertEqual(self.repo.get(claim["id"])["state"], "COMPLETED")
        self.transport.send_video.assert_called_once()

    def test_same_content_in_a_different_job_cannot_create_second_delivery(self):
        value, _root, _manifest, review, sig = self.fixture()
        claim = value["claim"]
        self.outbox.prepare(claim["id"], claim["lease_token"], 1, review, sig)
        self.repo.transition(claim["id"], claim["lease_token"], "FAILED")
        with patch(
            "status_bot.pipeline.fetch_weather", return_value=(copy.deepcopy(value["content"]), [])
        ):
            other, _, _, next_review, next_sig = self.fixture("wa_other_fixture")
        next_claim = other["claim"]
        with self.assertRaisesRegex(DeliveryBlocked, "WA_DUPLICATE_BLOCKED"):
            self.outbox.prepare(
                next_claim["id"], next_claim["lease_token"], 1, next_review, next_sig
            )

    def test_backup_preserves_delivery_unknown_and_blocks_replay(self):
        from studio_core.storage.backup import create_snapshot, probe_restore

        claim, _root, review, sig, item = self.queued()
        self.open_window()
        self.transport.send_video.side_effect = TimeoutError("unit fixture timeout")
        self.worker().deliver(item, claim["lease_token"], review, sig)
        backup_root = self.root / "backup"
        snapshot = create_snapshot(self.repo.path, backup_root, self.cfg.app, "e" * 40)
        self.assertEqual(
            probe_restore(snapshot, self.cfg.app, self.root / "restore-probe")["result"], "PASS"
        )
        import sqlite3

        with sqlite3.connect(snapshot / "jobs.sqlite") as db:
            self.assertEqual(
                db.execute("SELECT state FROM whatsapp_outbox WHERE id=?", (item,)).fetchone()[0],
                "DELIVERY_UNKNOWN",
            )

    def test_independent_watchdog_observes_unknown_without_mutation_or_secrets(self):
        from status_bot.whatsapp_watchdog import inspect_outbox

        claim, _root, review, sig, item = self.queued()
        self.open_window()
        self.transport.send_video.side_effect = TimeoutError("unit timeout")
        self.worker().deliver(item, claim["lease_token"], review, sig)
        timeline = self.repo.timeline(claim["id"])
        with patch("status_bot.whatsapp_egress.graph_post") as network:
            result = inspect_outbox(self.repo.path)
        network.assert_not_called()
        self.assertEqual(result["result"], "UNKNOWN")
        self.assertEqual(result["items"][0]["state"], "DELIVERY_UNKNOWN")
        self.assertEqual(result["production_acceptance"], "NOT_VERIFIED")
        self.assertFalse(result["delivery_authority"])
        self.assertEqual(self.repo.timeline(claim["id"]), timeline)
        for field in ("identity_binding", "message_tag", "lease_token", self.identity.recipient):
            self.assertNotIn(field, json.dumps(result))

    def test_watchdog_missing_database_or_table_is_unknown_and_creates_nothing(self):
        from status_bot.whatsapp_watchdog import inspect_outbox, main

        with patch("sys.stdout") as output:
            self.assertEqual(
                main(["--database", str(self.root / "missing-private-path.sqlite")]), 2
            )
        self.assertFalse((self.root / "missing-private-path.sqlite").exists())
        self.assertNotIn(
            "missing-private-path", "".join(call.args[0] for call in output.write.call_args_list)
        )
        other = Repository(replace(self.cfg, data_root=self.root / "fresh-state"))
        result = inspect_outbox(other.path)
        self.assertEqual(result["result"], "UNKNOWN")
        self.assertFalse(result["outbox_present"])

    def test_synthetic_and_sacred_content_are_not_eligible(self):
        value, root, manifest, _review, _sig = self.fixture()
        for key, val in [("synthetic_source", True), ("destination", "telegram")]:
            changed = copy.deepcopy(manifest)
            changed[key] = val
            (root / "manifest.json").write_text(json.dumps(changed))
            with self.assertRaises(DeliveryBlocked):
                self.outbox.bundle(value["claim"]["id"], 1)
        manifest["content"]["sacred"] = True
        (root / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(DeliveryBlocked):
            self.outbox.bundle(value["claim"]["id"], 1)

    def test_tampered_render_metadata_cannot_reuse_quality_success(self):
        value, root, manifest, _review, _sig = self.fixture()
        manifest["render"]["qa"]["claimed_extra_gate"] = "PASS"
        (root / "output/render-result.json").write_text(json.dumps(manifest["render"]))
        (root / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(DeliveryBlocked, "WA_QUALITY_LINEAGE_INVALID"):
            self.outbox.bundle(value["claim"]["id"], 1)

    def test_tampered_file_and_linked_parent_are_rejected(self):
        claim, root, review, sig, item = self.queued()
        self.open_window()
        (root / "output/weather.mp4").write_bytes(b"tampered")
        with self.assertRaises(DeliveryBlocked):
            self.worker().deliver(item, claim["lease_token"], review, sig)
        self.transport.upload_video.assert_not_called()
        actual = root / "actual-output"
        (root / "output").rename(actual)
        (root / "output").symlink_to(actual)
        with self.assertRaises(DeliveryBlocked):
            read_bound(self.cfg.data_root, "renders/" + claim["id"] + "/output/weather.mp4", 1000)

    def test_maintenance_lock_blocks_worker_before_transport(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()
        with ComponentLocks(self.repo).hold("whatsapp-delivery", "MAINTENANCE"):
            with self.assertRaises(ComponentBusy):
                self.worker().deliver(item, claim["lease_token"], review, sig)
        self.transport.upload_video.assert_not_called()

    def test_crash_intent_survives_reopen_and_blocks_replay(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()
        self.outbox.begin(item, claim["lease_token"], review, sig)
        self.outbox.send_intent(item, claim["lease_token"])
        reopened = WhatsAppOutbox(Repository(self.cfg), self.identity)
        with self.assertRaises(DeliveryBlocked):
            DeliveryWorker(reopened, self.transport, enabled=True).deliver(
                item, claim["lease_token"], review, sig
            )
        self.transport.upload_video.assert_not_called()
        self.transport.send_video.assert_not_called()
        self.assertEqual(reopened.receipt(item)["state"], "SEND_INTENT")

    def test_unsigned_wrong_account_phone_recipient_and_message_cannot_confirm(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()
        self.worker().deliver(item, claim["lease_token"], review, sig)
        raw, _signature = self.webhook(status="delivered")
        with self.assertRaises(DeliveryBlocked):
            self.outbox.webhook(raw, "sha256=" + "0" * 64)
        for scope in ({"phone": "99999999"}, {"business": "99999998"}):
            with self.assertRaises(DeliveryBlocked):
                self.outbox.webhook(*self.webhook(status="delivered", **scope))
        for scope in ({"recipient": "19999999999"}, {"message_id": "wamid.OTHER_FIXTURE"}):
            self.outbox.webhook(*self.webhook(status="delivered", **scope))
        self.assertEqual(self.outbox.receipt(item)["state"], "API_ACCEPTED")

    def test_replayed_inbound_does_not_extend_window_and_future_time_is_rejected(self):
        old = int(time.time()) - 100
        raw, sig = self.webhook(inbound=True, timestamp=old)
        self.outbox.webhook(raw, sig)
        self.outbox.webhook(raw, sig)
        with self.repo.connect() as db:
            self.assertEqual(
                db.execute("SELECT last_inbound_at FROM whatsapp_windows").fetchone()[0], old
            )
        with self.assertRaises(DeliveryBlocked):
            self.outbox.webhook(*self.webhook(inbound=True, timestamp=int(time.time()) + 3600))

    def test_secrets_phone_ids_and_raw_response_not_in_ledger_receipt_or_repr(self):
        claim, _root, review, sig, item = self.queued()
        self.open_window()
        self.worker().deliver(item, claim["lease_token"], review, sig)
        self.outbox.webhook(*self.webhook(status="read"))
        with self.repo.connect() as db:
            text = json.dumps(
                [
                    list(x)
                    for table in (
                        "events",
                        "whatsapp_outbox",
                        "whatsapp_windows",
                        "whatsapp_receipts",
                    )
                    for x in db.execute("SELECT * FROM " + table)
                ]
            )
        text += json.dumps(self.outbox.receipt(item)) + repr(self.identity)
        for secret in (
            self.identity.recipient,
            self.identity.phone_number,
            self.identity.business_account,
            self.identity.gate_key.decode(),
            self.identity.webhook_key.decode(),
            "wamid.UNIT_FIXTURE_ACK",
        ):
            self.assertNotIn(secret, text)

    def test_foreign_app_database_and_transport_identity_are_rejected(self):
        with self.assertRaises(DeliveryBlocked):
            WhatsAppOutbox(Repository(replace(self.cfg, app="snap-bot-v2")), self.identity)
        self.transport.credentials.recipient = "19999999999"
        with self.assertRaisesRegex(DeliveryBlocked, "WA_TRANSPORT_IDENTITY_MISMATCH"):
            self.worker()

    def test_strict_json_rejects_duplicate_keys_nan_and_oversize(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b" " * 1048577):
            with self.assertRaises(DeliveryBlocked):
                parse_json(raw)


class GraphTransportContracts(unittest.TestCase):
    def setUp(self):
        self.creds = GraphCredentials(
            "v25.0", "12345002", "10000000000", "unit-fixture-token-not-production"
        )

    def test_pinned_host_path_auth_post_one_call_and_no_redirect_follow(self):
        connection = Mock()
        response = Mock(status=302)
        response.read.return_value = b"{}"
        connection.getresponse.return_value = response
        with (
            patch(
                "status_bot.whatsapp_egress.validate_target",
                return_value=("graph.facebook.com", "8.8.8.8", "/v25.0/12345002/messages"),
            ) as target,
            patch(
                "status_bot.whatsapp_egress.PinnedHTTPSConnection", return_value=connection
            ) as connect,
        ):
            self.assertEqual(
                graph_post(self.creds, "messages", b"{}", "application/json").status, 302
            )
        target.assert_called_once_with(
            "https://graph.facebook.com/v25.0/12345002/messages", ("graph.facebook.com",)
        )
        connect.assert_called_once_with("graph.facebook.com", "8.8.8.8", 30)
        connection.request.assert_called_once()
        args, kwargs = connection.request.call_args
        self.assertEqual(args[:2], ("POST", "/v25.0/12345002/messages"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + self.creds.token)
        connection.close.assert_called_once()

    def test_dns_connection_http_and_close_errors_cannot_leak_credentials(self):
        connection = Mock()
        connection.request.side_effect = RuntimeError(self.creds.token)
        connection.close.side_effect = RuntimeError(self.creds.recipient)
        with (
            patch(
                "status_bot.whatsapp_egress.validate_target",
                return_value=("graph.facebook.com", "8.8.8.8", "/fixed"),
            ),
            patch("status_bot.whatsapp_egress.PinnedHTTPSConnection", return_value=connection),
        ):
            with self.assertRaisesRegex(DeliveryBlocked, "^WA_TRANSPORT_UNKNOWN$"):
                graph_post(self.creds, "messages", b"{}", "application/json")
        with (
            patch(
                "status_bot.whatsapp_egress.validate_target",
                side_effect=RuntimeError(self.creds.token),
            ),
            self.assertRaisesRegex(DeliveryBlocked, "^WA_TRANSPORT_UNKNOWN$"),
        ):
            graph_post(self.creds, "messages", b"{}", "application/json")

    def test_upload_is_local_bytes_multipart_and_send_has_media_id_no_link(self):
        transport = GraphTransport(self.creds, enabled=True)
        with patch(
            "status_bot.whatsapp_egress.graph_post", return_value=ApiResponse(200, b"{}")
        ) as post:
            transport.upload_video(b"unit-video")
            transport.send_video("123456")
        upload, send = post.call_args_list
        self.assertEqual(upload.args[1], "media")
        self.assertIn(b"unit-video", upload.args[2])
        self.assertIn(b'name="messaging_product"', upload.args[2])
        self.assertEqual(send.args[1], "messages")
        value = json.loads(send.args[2])
        self.assertEqual(value["video"], {"id": "123456"})
        self.assertEqual(value["to"], self.creds.recipient)
        self.assertNotIn("link", value["video"])

    def test_worker_has_no_untracked_network_path(self):
        import ast
        import inspect

        from status_bot import whatsapp_egress, whatsapp_outbox

        worker = ast.parse(inspect.getsource(whatsapp_egress.DeliveryWorker))
        calls = {ast.unparse(node.func) for node in ast.walk(worker) if isinstance(node, ast.Call)}
        self.assertFalse(
            any("socket" in name or "urlopen" in name or "HTTP" in name for name in calls)
        )
        # The ledger never imports a network/client or calls a sender.
        tree = ast.parse(inspect.getsource(whatsapp_outbox))
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertFalse(
            any(x and ("egress" in x or "network" in x or "http" in x) for x in imports)
        )

    def test_invalid_credentials_endpoint_and_oversized_video_never_connect(self):
        with self.assertRaises(DeliveryBlocked):
            GraphCredentials("latest", "12345002", "10000000000", self.creds.token)
        transport = GraphTransport(self.creds, enabled=True)
        with patch("status_bot.whatsapp_egress.validate_target") as target:
            with self.assertRaises(DeliveryBlocked):
                graph_post(self.creds, "getMe", b"{}", "application/json")
            with self.assertRaises(DeliveryBlocked):
                transport.upload_video(b"x" * 16000001)
            target.assert_not_called()


if __name__ == "__main__":
    unittest.main()
