"""Offline authenticated controller/HTTP tests; identities and video are fixtures.

Real AF_UNIX tests belong to the same suite and are not skipped in restricted
containers. No test opens a public socket or uses live Graph credentials.
"""

import hashlib
import hmac
import io
import json
import os
import struct
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import test_status_whatsapp_outbox as fixtures

from status_bot.owner_review import OwnerConfig, OwnerReview
from status_bot.review_http import COOKIE, Endpoint, Handler, PrivateServer, WebhookConfig, fields
from status_bot.review_service import assemble, main
from status_bot.whatsapp_contract import DeliveryBlocked
from studio_core.jobs import LeaseLost
from studio_core.network import UnixHTTPConnection


class FakeSocket:
    def __init__(self, request, uid):
        self.raw, self.uid, self.output = request, uid, []

    def getsockopt(self, *args):
        return struct.pack("3i", 123, self.uid, self.uid)

    def settimeout(self, value):
        pass

    def makefile(self, mode, buffering=-1):
        return io.BytesIO(self.raw)

    def sendall(self, value):
        self.output.append(bytes(value))


class ReviewContracts(unittest.TestCase):
    fixture = fixtures.WhatsAppContracts.fixture
    webhook = fixtures.WhatsAppContracts.webhook

    def setUp(self):
        fixtures.WhatsAppContracts.setUp(self)
        self.owner_config = OwnerConfig(
            "https://review.example.test",
            "unit-owner",
            b"unit-fixture-login-secret-32-bytes-only",
            b"unit-fixture-session-key-32-bytes-only",
        )
        self.controller = OwnerReview(self.outbox, self.owner_config)
        self.cookie = self.controller.login("unit-owner", self.owner_config.login_secret)
        self.csrf = self.controller.csrf(self.cookie)
        self.owner = Endpoint("owner", self.controller)
        self.receiver = Endpoint(
            "webhook",
            self.outbox,
            WebhookConfig("receipts.example.test", b"unit-fixture-verify-token-32-bytes-only"),
        )

    def ticket(self):
        value, root, manifest, _, _ = self.fixture()
        ticket = self.controller.open(self.cookie, self.csrf, value["claim"]["id"])
        return value["claim"], root, manifest, ticket

    def request(self, endpoint, method="GET", path="/", body=b"", headers=None, extra="", uid=1234):
        base = {"Host": endpoint.host}
        if endpoint.mode == "owner":
            base["Cookie"] = COOKIE + "=" + self.cookie
            if method == "POST":
                base.update(
                    {
                        "Origin": self.owner_config.origin,
                        "Content-Type": "application/x-www-form-urlencoded",
                    }
                )
        if method == "POST":
            base["Content-Length"] = str(len(body))
        if headers:
            base.update(headers)
        raw = (
            method
            + " "
            + path
            + " HTTP/1.1\r\n"
            + "".join(k + ": " + v + "\r\n" for k, v in base.items())
            + extra
            + "\r\n"
        ).encode() + body
        connection = FakeSocket(raw, uid)
        server = SimpleNamespace(endpoint=endpoint, proxy_uid=1234)
        Handler(connection, "", server)
        output = b"".join(connection.output)
        head, _, data = output.partition(b"\r\n\r\n")
        return int(head.split(b" ")[1]), head.decode(), data

    def test_private_config_requires_https_distinct_keys_and_hidden_repr(self):
        for origin in (
            "http://review.example.test",
            "https://review.example.test/",
            "https://user:secret@review.example.test",
            "https://review.example.test?x=1",
        ):
            with self.assertRaises(DeliveryBlocked):
                replace(self.owner_config, origin=origin)
        with self.assertRaises(DeliveryBlocked):
            OwnerReview(self.outbox, replace(self.owner_config, session_key=self.identity.gate_key))
        self.assertNotIn("unit-fixture", repr(self.owner_config))
        self.assertNotIn("unit-fixture", repr(self.receiver.config))

    def test_login_budget_survives_recreation_and_changing_username(self):
        for index in range(5):
            with self.assertRaises(DeliveryBlocked):
                self.controller.login("wrong" + str(index), b"bad")
        recreated = OwnerReview(self.outbox, self.owner_config)
        with self.assertRaises(DeliveryBlocked):
            recreated.login("unit-owner", self.owner_config.login_secret)
        with self.repo.transaction() as db:
            db.execute("UPDATE owner_login_budget SET began_at=?", (time.time() - 301,))
        recreated.session(recreated.login("unit-owner", self.owner_config.login_secret))

    def test_session_stored_as_tag_and_expiry_forgery_rotation_blocked(self):
        with self.repo.connect() as db:
            row = dict(db.execute("SELECT * FROM owner_sessions").fetchone())
        self.assertNotIn(self.cookie, json.dumps(row))
        for value in ("0" * 64, self.cookie[:-1], None):
            with self.assertRaises(DeliveryBlocked):
                self.controller.session(value)
        rotated = OwnerReview(
            self.outbox,
            replace(self.owner_config, session_key=b"unit-fixture-other-session-key-32bytes"),
        )
        with self.assertRaises(DeliveryBlocked):
            rotated.session(self.cookie)
        with self.repo.transaction() as db:
            db.execute("UPDATE owner_sessions SET expires_at=0")
        with self.assertRaises(DeliveryBlocked):
            self.controller.session(self.cookie)

    def test_logout_revokes_session_and_preexisting_open_ticket(self):
        _, _, _, ticket = self.ticket()
        self.controller.logout(self.cookie, self.csrf)
        with self.assertRaises(DeliveryBlocked):
            self.controller.view(self.cookie, ticket)

    def test_new_owner_session_explicitly_opens_fresh_ticket_without_reusing_attestation(self):
        claim, _, _, ticket = self.ticket()
        self.controller.media(self.cookie, ticket)
        other = self.controller.login("unit-owner", self.owner_config.login_secret)
        csrf = self.controller.csrf(other)
        fresh = self.controller.open(other, csrf, claim["id"])
        self.assertNotEqual(fresh, ticket)
        with self.assertRaises(DeliveryBlocked):
            self.controller.view(self.cookie, ticket)
        with self.assertRaisesRegex(DeliveryBlocked, "REVIEW_MEDIA_REQUIRED"):
            self.controller.decide(other, csrf, fresh, "approve", True)

    def test_csrf_wrong_session_and_forgery_rejected(self):
        claim, _, _, ticket = self.ticket()
        other = self.controller.login("unit-owner", self.owner_config.login_secret)
        for cookie, csrf in ((self.cookie, "0" * 64), (other, self.csrf)):
            with self.assertRaises(DeliveryBlocked):
                self.controller.decide(cookie, csrf, ticket, "approve", True)
        with self.assertRaises(DeliveryBlocked):
            self.controller.view(other, ticket)
        self.assertEqual(self.repo.get(claim["id"])["state"], "VALIDATING")

    def test_pending_is_allowlisted_and_open_is_idempotent_for_same_session(self):
        claim, _, _, ticket = self.ticket()
        self.assertEqual(self.controller.pending(self.cookie)[0]["id"], claim["id"])
        self.assertEqual(self.controller.open(self.cookie, self.csrf, claim["id"]), ticket)
        self.assertEqual(set(self.controller.pending(self.cookie)[0]), {"id", "created_at"})

    def test_media_and_attestation_are_separate_requirements(self):
        claim, root, _, ticket = self.ticket()
        with self.assertRaisesRegex(DeliveryBlocked, "REVIEW_MEDIA_REQUIRED"):
            self.controller.decide(self.cookie, self.csrf, ticket, "approve", True)
        self.assertEqual(
            self.controller.media(self.cookie, ticket), (root / "output/weather.mp4").read_bytes()
        )
        with self.assertRaisesRegex(DeliveryBlocked, "REVIEW_ATTESTATION_REQUIRED"):
            self.controller.decide(self.cookie, self.csrf, ticket, "approve")
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM whatsapp_outbox").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM owner_reviews").fetchone()[0], "OPEN")
        self.assertEqual(self.repo.get(claim["id"])["state"], "VALIDATING")

    def test_approval_commits_ticket_and_outbox_without_network(self):
        claim, _, _, ticket = self.ticket()
        self.controller.media(self.cookie, ticket)
        with patch("socket.socket", side_effect=AssertionError("Unexpected network")):
            result = self.controller.decide(self.cookie, self.csrf, ticket, "approve", True)
        self.assertEqual(result["state"], "PREPARED")
        self.assertFalse(result["recipient_delivery_confirmed"])
        self.assertEqual(self.repo.get(claim["id"])["state"], "VALIDATING")
        review, sig = self.controller.approved_material(result["id"])
        self.assertEqual(review["video_sha256"], result["video_sha256"])
        self.assertEqual(len(sig), 64)
        self.assertNotIn(self.identity.recipient, json.dumps(review))
        self.transport.send_video.assert_not_called()
        self.transport.upload_video.assert_not_called()
        with self.assertRaises(DeliveryBlocked):
            self.controller.decide(self.cookie, self.csrf, ticket, "approve", True)

    def test_approval_rollback_leaves_ticket_open_and_no_outbox(self):
        _, _, _, ticket = self.ticket()
        self.controller.media(self.cookie, ticket)
        original = self.repo.event

        def broken(db, job, event, details):
            if event == "whatsapp.prepared":
                raise RuntimeError("fixture exception SECRET")
            return original(db, job, event, details)

        with patch.object(self.repo, "event", side_effect=broken):
            with self.assertRaises(RuntimeError):
                self.controller.decide(self.cookie, self.csrf, ticket, "approve", True)
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM owner_reviews").fetchone()[0], "OPEN")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM whatsapp_outbox").fetchone()[0], 0)
            self.assertFalse(
                db.execute("SELECT 1 FROM events WHERE event='owner.review_approved'").fetchone()
            )

    def test_session_expiring_between_validation_and_commit_blocks_approval(self):
        _, _, _, ticket = self.ticket()
        self.controller.media(self.cookie, ticket)
        original = self.outbox.bundle
        calls = 0

        def expire(job, attempt):
            nonlocal calls
            value = original(job, attempt)
            calls += 1
            if calls == 2:
                with self.repo.transaction() as db:
                    db.execute("UPDATE owner_sessions SET expires_at=0")
            return value

        with patch.object(self.outbox, "bundle", side_effect=expire):
            with self.assertRaisesRegex(DeliveryBlocked, "REVIEW_SESSION_DENIED"):
                self.controller.decide(self.cookie, self.csrf, ticket, "approve", True)
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM owner_reviews").fetchone()[0], "OPEN")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM whatsapp_outbox").fetchone()[0], 0)

    def test_concurrent_approvals_have_one_effect(self):
        _, _, _, ticket = self.ticket()
        self.controller.media(self.cookie, ticket)

        def approve():
            try:
                return self.controller.decide(self.cookie, self.csrf, ticket, "approve", True)[
                    "state"
                ]
            except DeliveryBlocked:
                return "BLOCKED"

        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(
                sorted(pool.map(lambda _: approve(), range(2))), ["BLOCKED", "PREPARED"]
            )
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM whatsapp_outbox").fetchone()[0], 1)

    def test_rejection_cancels_only_current_job_and_cannot_be_replayed(self):
        claim, _, _, ticket = self.ticket()
        self.assertEqual(
            self.controller.decide(self.cookie, self.csrf, ticket, "reject"), {"state": "REJECTED"}
        )
        self.assertEqual(self.repo.get(claim["id"])["state"], "CANCELLED")
        with self.assertRaises(DeliveryBlocked):
            self.controller.decide(self.cookie, self.csrf, ticket, "reject")
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM whatsapp_outbox").fetchone()[0], 0)

    def test_changed_media_manifest_and_expired_ticket_block(self):
        _, root, manifest, ticket = self.ticket()
        path = root / "output/weather.mp4"
        original = path.read_bytes()
        path.write_bytes(b"changed unit fixture")
        with self.assertRaises(DeliveryBlocked):
            self.controller.media(self.cookie, ticket)
        path.write_bytes(original)
        path = root / "manifest.json"
        original = path.read_bytes()
        changed = dict(manifest)
        changed["unreviewed_field"] = "changed"
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(DeliveryBlocked, "REVIEW_ARTIFACT_CHANGED"):
            self.controller.view(self.cookie, ticket)
        path.write_bytes(original)
        with self.repo.transaction() as db:
            db.execute("UPDATE owner_reviews SET expires_at=0")
        with self.assertRaises(DeliveryBlocked):
            self.controller.view(self.cookie, ticket)

    def test_cancelled_or_reclaimed_attempt_blocks_review_without_claiming(self):
        claim, _, _, ticket = self.ticket()
        with self.repo.transaction() as db:
            db.execute("UPDATE jobs SET lease_until=0")
        with self.assertRaises(LeaseLost):
            self.controller.media(self.cookie, ticket)
        next_claim = self.repo.claim(claim["id"])
        self.assertEqual(next_claim["attempt"], 2)
        with self.assertRaises(DeliveryBlocked):
            self.controller.decide(self.cookie, self.csrf, ticket, "approve", True)

    def test_identity_rotation_invalidates_open_ticket(self):
        _, _, _, ticket = self.ticket()
        original = self.outbox.identity
        self.outbox.identity = replace(original, recipient="10000000001")
        with self.assertRaisesRegex(DeliveryBlocked, "REVIEW_IDENTITY_CHANGED"):
            self.controller.view(self.cookie, ticket)

    def test_expired_or_tampered_grant_is_not_reissued(self):
        _, _, _, ticket = self.ticket()
        self.controller.media(self.cookie, ticket)
        result = self.controller.decide(self.cookie, self.csrf, ticket, "approve", True)
        with self.repo.transaction() as db:
            db.execute("UPDATE owner_reviews SET approved_review='{}'")
        with self.assertRaisesRegex(DeliveryBlocked, "REVIEW_GRANT_CHANGED"):
            self.controller.approved_material(result["id"])

    def test_expired_approval_is_not_extended_by_login_or_local_material_read(self):
        _, _, _, ticket = self.ticket()
        self.controller.media(self.cookie, ticket)
        result = self.controller.decide(self.cookie, self.csrf, ticket, "approve", True)
        original_clock = self.repo.clock
        with patch.object(self.repo, "clock", return_value=original_clock() + 301):
            with self.assertRaisesRegex(DeliveryBlocked, "WA_REVIEW_EXPIRED"):
                self.controller.approved_material(result["id"])
        self.assertEqual(self.outbox.receipt(result["id"])["state"], "PREPARED")

    def test_foreign_database_job_denies_session_and_all_control(self):
        _claim, _, _, ticket = self.ticket()
        with self.repo.transaction() as db:
            db.execute("UPDATE jobs SET app='snap-bot-v2'")
        with self.assertRaisesRegex(DeliveryBlocked, "WA_DATABASE_SCOPE_INVALID"):
            self.controller.session(self.cookie)
        self.assertEqual(self.request(self.owner, path="/review/" + ticket)[0], 403)

    def test_ui_shows_escaped_editorial_content_without_internal_metadata(self):
        claim, _, _, ticket = self.ticket()
        status, head, body = self.request(self.owner, path="/review/" + ticket)
        self.assertEqual(status, 200)
        self.assertIn("Content-Security-Policy:", head)
        self.assertIn("frame-ancestors", head)
        self.assertIn("no-store", head)
        self.assertIn('dir="rtl"', body.decode())
        self.assertIn("شاهدت المقطع", body.decode())
        for value in (
            claim["lease_token"],
            self.identity.recipient,
            "identity_binding",
            "content_hash",
            "network_isolation",
        ):
            self.assertNotIn(value, body.decode())
        row, owned, manifest, video = self.controller.view(self.cookie, ticket)
        manifest["content"]["title"] = "<script>fixture</script>"
        with patch.object(self.controller, "view", return_value=(row, owned, manifest, video)):
            body = self.request(self.owner, path="/review/" + ticket)[2]
        self.assertIn(b"&lt;script&gt;", body)
        self.assertNotIn(b"<script>", body)

    def test_http_login_sets_secure_host_cookie_and_requires_origin(self):
        body = urlencode(
            {"owner": "unit-owner", "secret": self.owner_config.login_secret.decode()}
        ).encode()
        status, head, _ = self.request(self.owner, "POST", "/login", body)
        self.assertEqual(status, 303)
        for value in ("__Host-", "Secure", "HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=900"):
            self.assertIn(value, head)
        self.assertNotIn("Domain=", head)
        self.assertEqual(
            self.request(
                self.owner, "POST", "/login", body, {"Origin": "https://evil.example.test"}
            )[0],
            403,
        )

    def test_http_post_routes_and_csrf_cannot_be_bypassed(self):
        claim, _, _, ticket = self.ticket()
        for target in ("/review", "/logout", "/review/" + ticket + "/decision"):
            body = (
                urlencode({"csrf": "0" * 64, "job": claim["id"]}).encode()
                if target == "/review"
                else b"csrf=bad"
            )
            self.assertEqual(self.request(self.owner, "POST", target, body)[0], 403)
        body = urlencode({"csrf": self.csrf, "job": claim["id"]}).encode()
        self.assertEqual(self.request(self.owner, "POST", "/review", body)[0], 303)
        self.assertEqual(
            self.request(self.owner, path="/review/" + ticket + "/media")[2],
            b"unit-fixture-bytes-not-an-actual-mp4",
        )
        body = urlencode({"csrf": self.csrf, "decision": "approve", "watched": "yes"}).encode()
        status, _, data = self.request(self.owner, "POST", "/review/" + ticket + "/decision", body)
        self.assertEqual(status, 200)
        self.assertIn("تم اعتماد المقطع", data.decode())
        self.assertNotIn("APPROVE_WHATSAPP_VIDEO", data.decode())

    def test_http_rejects_host_duplicates_cookie_forgery_and_public_send_route(self):
        for headers, extra in (
            ({"Host": "evil.example.test"}, ""),
            ({}, "Host: evil.example.test\r\n"),
            ({"Cookie": COOKIE + "=" + "0" * 64}, ""),
            ({"Cookie": COOKIE + "=" + self.cookie + "; " + COOKIE + "=" + self.cookie}, ""),
        ):
            self.assertEqual(self.request(self.owner, headers=headers, extra=extra)[0], 403)
        for path in (
            "/send",
            "/dispatch",
            "/api",
            "/whatsapp/webhook",
            "https://review.example.test/",
            "/?next=evil",
        ):
            self.assertEqual(self.request(self.owner, path=path)[0], 403)

    def test_http_framing_rejects_smuggling_duplicates_and_oversize(self):
        for headers, extra in (
            ({"Transfer-Encoding": "chunked"}, ""),
            ({}, "Content-Length: 1\r\n"),
            ({"Content-Length": "-1"}, ""),
            ({"Content-Length": "9000"}, ""),
            ({"Content-Encoding": "gzip"}, ""),
            ({"Expect": "100-continue"}, ""),
            ({"Content-Length": "8"}, ""),
        ):
            self.assertEqual(
                self.request(self.owner, "POST", "/login", b"foo=bar", headers, extra)[0], 403
            )
        self.assertEqual(self.request(self.owner, headers={"Content-Length": "1"})[0], 403)

    def test_form_rejects_duplicate_unknown_and_malformed_unicode_fields(self):
        for raw in (b"x=a&x=b", b"x=%GG", b"x=%ff", b"x=1&broken", b"x=" + b"a" * 9000):
            with self.assertRaises(DeliveryBlocked):
                fields(raw)
        self.assertEqual(
            self.request(self.owner, "POST", "/login", b"owner=x&secret=y&extra=z")[0], 403
        )

    def test_http_errors_do_not_expose_logs_secrets_or_exception(self):
        with patch.object(
            self.controller, "pending", side_effect=RuntimeError("TOKEN_SECRET /private/path")
        ):
            status, head, body = self.request(self.owner)
        self.assertEqual(status, 503)
        self.assertEqual(body, b"Service unavailable")
        self.assertNotIn("TOKEN_SECRET", head)
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.request(self.owner, "POST", "/login", b"secret=TOKEN_SECRET")
            self.assertEqual(stderr.getvalue(), "")

    def test_wrong_peer_denied_before_request_body_or_auth(self):
        with patch.object(self.controller, "login") as login:
            with self.assertRaisesRegex(DeliveryBlocked, "HTTP_PEER_DENIED"):
                self.request(self.owner, "POST", "/login", b"anything", uid=999)
        login.assert_not_called()

    def test_webhook_handshake_exact_challenge_and_no_mutations(self):
        token = self.receiver.config.verify_token.decode()
        path = "/whatsapp/webhook?" + urlencode(
            {"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": "1234567"}
        )
        status, _, body = self.request(self.receiver, path=path)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"1234567")
        for value in (
            path + "&hub.challenge=8",
            path.replace(token, "wrong"),
            path.replace("1234567", "evil"),
        ):
            self.assertEqual(self.request(self.receiver, path=value)[0], 403)
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM whatsapp_windows").fetchone()[0], 0)

    def test_webhook_accepts_signed_original_bytes_and_durable_replay(self):
        raw, sig = self.webhook(inbound=True)
        headers = {"Content-Type": "application/json", "X-Hub-Signature-256": sig}
        for _ in range(2):
            self.assertEqual(
                self.request(self.receiver, "POST", "/whatsapp/webhook", raw, headers)[0], 200
            )
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM whatsapp_windows").fetchone()[0], 1)
        self.transport.send_video.assert_not_called()

    def test_webhook_signature_uses_utf8_bytes_without_decoding_or_reserialization(self):
        raw, _ = self.webhook(inbound=True)
        raw = raw.replace(b"unit fixture", "اختبار إيصال".encode())
        sig = "sha256=" + hmac.new(self.identity.webhook_key, raw, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Hub-Signature-256": sig}
        self.assertEqual(
            self.request(self.receiver, "POST", "/whatsapp/webhook", raw, headers)[0], 200
        )
        alternate = json.dumps(json.loads(raw), ensure_ascii=True).encode()
        self.assertEqual(
            self.request(self.receiver, "POST", "/whatsapp/webhook", alternate, headers)[0], 403
        )

    def test_bad_signature_body_scope_and_receiver_routes_have_no_effect(self):
        raw, sig = self.webhook(inbound=True)
        for data, signature in (
            (raw, "sha256=" + "0" * 64),
            (raw + b" ", sig),
            self.webhook(inbound=True, phone="12345999"),
        ):
            self.assertEqual(
                self.request(
                    self.receiver,
                    "POST",
                    "/whatsapp/webhook",
                    data,
                    {"Content-Type": "application/json", "X-Hub-Signature-256": signature},
                )[0],
                403,
            )
        for path in ("/", "/login", "/send", "/review"):
            self.assertEqual(self.request(self.receiver, path=path)[0], 403)
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM whatsapp_windows").fetchone()[0], 0)

    def test_webhook_ack_only_after_commit_and_failure_is_generic(self):
        raw, sig = self.webhook(inbound=True)
        with patch.object(self.outbox, "webhook", side_effect=OSError("fixture SECRET")):
            status, _, body = self.request(
                self.receiver,
                "POST",
                "/whatsapp/webhook",
                raw,
                {"Content-Type": "application/json", "X-Hub-Signature-256": sig},
            )
        self.assertEqual(status, 503)
        self.assertNotIn(b"SECRET", body)

    def test_webhook_rejects_duplicate_signature_and_unsupported_encoding(self):
        raw, sig = self.webhook(inbound=True)
        headers = {"Content-Type": "application/json", "X-Hub-Signature-256": sig}
        self.assertEqual(
            self.request(
                self.receiver,
                "POST",
                "/whatsapp/webhook",
                raw,
                headers,
                "X-Hub-Signature-256: " + sig + "\r\n",
            )[0],
            403,
        )
        headers["Content-Encoding"] = "gzip"
        self.assertEqual(
            self.request(self.receiver, "POST", "/whatsapp/webhook", raw, headers)[0], 403
        )

    def test_server_disabled_by_default_and_rejects_unsafe_parent(self):
        with self.assertRaisesRegex(DeliveryBlocked, "HTTP_SERVICE_DISABLED"):
            PrivateServer(self.root / "owner.sock", self.owner, os.getuid())
        self.root.chmod(0o755)
        with self.assertRaisesRegex(DeliveryBlocked, "HTTP_SOCKET_INVALID"):
            PrivateServer(self.root / "owner.sock", self.owner, os.getuid(), enabled=True)

    def assembly_inputs(self, mode="owner"):
        config_path = self.root / "listener.toml"
        original = (Path(__file__).parents[3] / "config/status/weather.toml").read_text()
        config_path.write_text(
            original.replace(
                'data_root = "../.status-weather-state"',
                'data_root = "' + str(self.root / "assembled-state") + '"',
            )
        )
        settings_path = self.root / "listener-settings.json"
        settings_path.write_text(
            json.dumps(
                {"origin": self.owner_config.origin, "owner": "unit-owner"}
                if mode == "owner"
                else {"host": self.receiver.host}
            )
        )
        env = {
            "STATUS_WA_BUSINESS_ACCOUNT": self.identity.business_account,
            "STATUS_WA_PHONE_NUMBER": self.identity.phone_number,
            "STATUS_WA_RECIPIENT": self.identity.recipient,
            "STATUS_WA_GATE_KEY": self.identity.gate_key.decode(),
            "STATUS_WA_WEBHOOK_KEY": self.identity.webhook_key.decode(),
            "STATUS_OWNER_LOGIN_SECRET": self.owner_config.login_secret.decode(),
            "STATUS_OWNER_SESSION_KEY": self.owner_config.session_key.decode(),
            "STATUS_WA_VERIFY_TOKEN": self.receiver.config.verify_token.decode(),
        }
        return config_path, settings_path, env

    def test_explicit_assembly_needs_no_graph_token_and_separates_endpoints(self):
        cfg, settings, env = self.assembly_inputs()
        endpoint = assemble("owner", cfg, settings, env)
        self.assertEqual(endpoint.mode, "owner")
        self.assertEqual(endpoint.host, self.owner.host)
        cfg, settings, env = self.assembly_inputs("webhook")
        for key in ("STATUS_OWNER_LOGIN_SECRET", "STATUS_OWNER_SESSION_KEY"):
            env.pop(key)
        endpoint = assemble("webhook", cfg, settings, env)
        self.assertEqual(endpoint.mode, "webhook")
        with endpoint.service.repo.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM whatsapp_outbox").fetchone()[0], 0)

    def test_missing_or_duplicate_assembly_keys_block_before_state_creation(self):
        cfg, settings, env = self.assembly_inputs()
        env.pop("STATUS_OWNER_LOGIN_SECRET")
        with self.assertRaises(DeliveryBlocked):
            assemble("owner", cfg, settings, env)
        self.assertFalse((self.root / "assembled-state").exists())
        env["STATUS_OWNER_LOGIN_SECRET"] = env["STATUS_WA_GATE_KEY"]
        with self.assertRaises(DeliveryBlocked):
            assemble("owner", cfg, settings, env)
        self.assertFalse((self.root / "assembled-state").exists())

    def test_listener_default_never_assembles_or_reads_environment(self):
        arguments = [
            "--mode",
            "owner",
            "--config",
            "fixture",
            "--settings",
            "fixture",
            "--socket",
            "fixture",
            "--proxy-uid",
            "1234",
        ]
        with (
            patch("status_bot.review_service.assemble") as factory,
            patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            self.assertEqual(main(arguments, {}), 2)
            self.assertEqual(err.getvalue(), "REVIEW_SERVICE_BLOCKED\n")
        factory.assert_not_called()

    def test_listener_closes_without_auto_unlink_or_restart(self):
        args = [
            "--mode",
            "owner",
            "--config",
            "fixture",
            "--settings",
            "fixture",
            "--socket",
            "fixture",
            "--proxy-uid",
            "1234",
            "--enable-listener",
        ]
        with (
            patch("status_bot.review_service.assemble", return_value=self.owner),
            patch("status_bot.review_service.PrivateServer") as factory,
        ):
            factory.return_value.serve_forever.side_effect = KeyboardInterrupt
            self.assertEqual(main(args, {}), 0)
        factory.return_value.server_close.assert_called_once()

    def test_real_unix_owner_server_and_stale_socket_not_replaced(self):
        self.root.chmod(0o700)
        path = self.root / "owner.sock"
        server = PrivateServer(path, self.owner, os.getuid(), enabled=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = UnixHTTPConnection(path, 5)
            try:
                connection.request("GET", "/login", headers={"Host": self.owner.host})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("دخول المالك", response.read().decode())
            finally:
                connection.close()
            with self.assertRaisesRegex(DeliveryBlocked, "HTTP_SOCKET_INVALID"):
                PrivateServer(path, self.owner, os.getuid(), enabled=True)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()

    def test_real_unix_webhook_server_verified_handshake(self):
        self.root.chmod(0o700)
        server = PrivateServer(self.root / "hook.sock", self.receiver, os.getuid(), enabled=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = UnixHTTPConnection(self.root / "hook.sock", 5)
            path = "/whatsapp/webhook?" + urlencode(
                {
                    "hub.mode": "subscribe",
                    "hub.verify_token": self.receiver.config.verify_token.decode(),
                    "hub.challenge": "54321",
                }
            )
            try:
                connection.request("GET", path, headers={"Host": self.receiver.host})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"54321")
            finally:
                connection.close()
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()
