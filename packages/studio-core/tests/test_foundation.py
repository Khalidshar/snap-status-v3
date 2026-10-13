import base64
import hashlib
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from studio_core.config import Config
from studio_core.jobs import Conflict, GatewayLimit, LeaseLost, Repository
from studio_core.jobs.gateway import CommandGateway, Unauthorized
from studio_core.network import NetworkClient, NetworkError
from studio_core.network.egress import EgressServer, PolicyDenied, fetch_https, validate_target
from studio_core.observability import Redactor
from studio_core.storage import Storage


class Clock:
    value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = Config(
            "snap-bot-v2",
            root,
            {"scheduler": ("owner",)},
            ("demo",),
            {"CRITICAL": 180, "HIGH": 600, "NORMAL": 600, "BACKGROUND": 1200},
            30,
            60,
            10,
            3,
            60,
            60,
            86400,
            root / "proxy.sock",
            ("allowed.example",),
            2,
            4096,
            1,
        )
        self.clock = Clock()
        self.secret = "test-signing-value-never-production-" * 2
        self.repo = Repository(self.config, Redactor([self.secret]), self.clock)
        self.gateway = CommandGateway(self.config, self.repo, self.secret, self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, event="e1", **updates):
        body = {
            "channel": "scheduler",
            "actor": "owner",
            "command": "demo",
            "event_id": event,
            "priority": "NORMAL",
            **updates,
        }
        return self.gateway.accept(body, self.clock(), self.gateway.signature(body, self.clock()))

    def complete(self, claim, state="COMPLETED"):
        if state in ("FAILED", "CANCELLED"):
            self.repo.transition(claim["id"], claim["lease_token"], state)
            return
        for phase in (
            "RESEARCHING",
            "PROCESSING",
            "DOWNLOADING",
            "DESIGNING",
            "RENDERING",
            "VALIDATING",
            "REPAIRING",
            "VALIDATING",
            "DELIVERING",
            "COMPLETED",
        ):
            self.repo.transition(claim["id"], claim["lease_token"], phase)

    def test_idempotency_same_event_creates_one_job(self):
        first, created = self.submit()
        second, again = self.submit()
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first["id"], second["id"])

    def test_idempotency_conflicting_payload_rejected(self):
        self.submit()
        with self.assertRaises(Conflict):
            self.submit(priority="HIGH")

    def test_other_actor_is_rejected_even_with_valid_signature(self):
        with self.assertRaises(Unauthorized):
            self.submit(actor="stranger")
        self.assertIsNone(self.repo.claim())

    def test_bad_signature_does_not_enqueue(self):
        with self.assertRaises(Unauthorized):
            self.gateway.accept({}, self.clock(), "0" * 64)
        self.assertIsNone(self.repo.claim())

    def test_old_signature_rejected(self):
        body = {"channel": "scheduler", "actor": "owner", "event_id": "e", "command": "demo"}
        with self.assertRaises(Unauthorized):
            self.gateway.accept(body, 100, self.gateway.signature(body, 100))

    def test_malformed_command_types_rejected(self):
        for key in ("command", "priority", "event_id"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.submit(**{key: []})

    def test_unknown_command_and_fields_rejected(self):
        with self.assertRaises(ValueError):
            self.submit(command="publish")
        with self.assertRaises(ValueError):
            self.submit(token="never-store-me")  # noqa: S106  (synthetic secret under test)

    def test_expired_command_rejected(self):
        with self.assertRaises(ValueError):
            self.submit(expires_at=self.clock() - 1)

    def test_priority_caps_requested_deadline(self):
        job, _ = self.submit(priority="CRITICAL", expires_at=99999)
        self.assertEqual(job["expires_at"], self.clock() + 180)

    def test_rate_limit_not_bypassed_by_completed_jobs(self):
        config = replace(self.config, rate_limit=2)
        self.repo.config = config
        for event in ("e1", "e2"):
            self.submit(event)
            self.complete(self.repo.claim())
        with self.assertRaises(GatewayLimit):
            self.submit("e3")

    def test_active_job_limit_and_expired_jobs(self):
        self.repo.config = replace(self.config, max_active_per_actor=1)
        self.submit()
        with self.assertRaises(GatewayLimit):
            self.submit("e2")
        self.clock.advance(601)
        self.submit("e3")
        self.assertIsNotNone(self.repo.claim())

    def test_concurrent_claim_has_one_lease(self):
        self.submit()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.repo.claim(), range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_global_resource_limit_waits_in_queue(self):
        self.submit()
        self.submit("e2")
        first = self.repo.claim()
        self.assertIsNone(self.repo.claim())
        self.complete(first)
        self.assertIsNotNone(self.repo.claim())

    def test_expired_lease_recovered_and_old_worker_fenced(self):
        job, _ = self.submit()
        old = self.repo.claim()
        self.clock.advance(31)
        new = self.repo.claim()
        self.assertEqual(job["id"], new["id"])
        self.assertNotEqual(old["lease_token"], new["lease_token"])
        with self.assertRaises(LeaseLost):
            self.repo.transition(job["id"], old["lease_token"], "RESEARCHING")

    def test_heartbeat_renews_only_current_unexpired_lease(self):
        self.submit()
        claim = self.repo.claim()
        self.clock.advance(20)
        self.repo.heartbeat(claim["id"], claim["lease_token"])
        self.clock.advance(20)
        self.assertIsNone(self.repo.claim())
        with self.assertRaises(LeaseLost):
            self.repo.heartbeat(claim["id"], "wrong")

    def test_expiry_including_queue_wait(self):
        job, _ = self.submit(priority="CRITICAL")
        self.clock.advance(181)
        self.assertIsNone(self.repo.claim())
        self.assertEqual(self.repo.get(job["id"])["state"], "EXPIRED")

    def test_priority_and_aging(self):
        background, _ = self.submit("old", priority="BACKGROUND")
        self.clock.advance(241)
        self.submit("urgent", priority="CRITICAL")
        self.assertEqual(self.repo.claim()["id"], background["id"])

    def test_invalid_jump_cannot_skip_validation(self):
        self.submit()
        claim = self.repo.claim()
        with self.assertRaises(ValueError):
            self.repo.transition(claim["id"], claim["lease_token"], "DELIVERING")

    def test_demo_traverses_repair_loop_and_completes(self):
        self.submit()
        claim = self.repo.claim()
        self.complete(claim)
        states = [json.loads(e["details"]).get("state") for e in self.repo.timeline(claim["id"])]
        self.assertEqual(states.count("VALIDATING"), 2)
        self.assertIn("REPAIRING", states)
        self.assertEqual(self.repo.get(claim["id"])["state"], "COMPLETED")

    def test_cancel_and_failure_are_terminal(self):
        for event, state in [("e1", "FAILED"), ("e2", "CANCELLED")]:
            self.submit(event)
            claim = self.repo.claim()
            self.complete(claim, state)
            with self.assertRaises(LeaseLost):
                self.repo.transition(claim["id"], claim["lease_token"], "RESEARCHING")

    def test_uncertain_delivery_never_automatically_replayed(self):
        job, _ = self.submit()
        claim = self.repo.claim()
        for state in (
            "RESEARCHING",
            "PROCESSING",
            "DOWNLOADING",
            "DESIGNING",
            "RENDERING",
            "VALIDATING",
            "DELIVERING",
        ):
            self.repo.transition(job["id"], claim["lease_token"], state)
        self.clock.advance(601)
        self.assertIsNone(self.repo.claim())
        result = self.repo.get(job["id"])
        self.assertEqual(result["error_code"], "DELIVERY_UNKNOWN")
        self.assertIsNone(result["artifacts_terminal_at"])

    def test_retention_waits_for_delivery_terminal_plus_grace(self):
        job, _ = self.submit()
        self.complete(self.repo.claim())
        storage = Storage(self.config)
        storage.manifest(job["id"], {"synthetic": True})
        root = storage.workspace(job["id"])
        (root / "input" / "source.json").write_text("{}")
        self.clock.advance(90000)
        self.assertEqual(storage.gc(self.repo), [])
        with self.repo.transaction() as db:
            db.execute("INSERT INTO deliveries VALUES('d',?,'MANUAL_SENT')", (job["id"],))
        with self.assertRaises(ValueError):
            self.repo.mark_artifacts_terminal(job["id"], "MANUAL_CONFIRMED")
        with self.repo.transaction() as db:
            db.execute("UPDATE deliveries SET state='MANUAL_CONFIRMED'")
        self.repo.mark_artifacts_terminal(job["id"], "MANUAL_CONFIRMED")
        self.clock.advance(86399)
        self.assertEqual(storage.gc(self.repo), [])
        self.clock.advance(2)
        self.assertEqual(storage.gc(self.repo), [job["id"]])
        self.assertTrue((storage.metadata / (job["id"] + ".json")).exists())

    def test_workspace_rejects_traversal_and_symlink(self):
        storage = Storage(self.config)
        with self.assertRaises(ValueError):
            storage.workspace("../outside")
        target = Path(self.temp.name) / "outside"
        target.mkdir()
        storage.root.mkdir()
        (storage.root / ("a" * 32)).symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            storage.workspace("a" * 32)

    def test_database_backup_preserves_queue_and_dedup(self):
        job, _ = self.submit()
        backup = Path(self.temp.name) / "backup.sqlite"
        self.repo.backup(backup)
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute("SELECT id FROM jobs").fetchone()[0], job["id"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM commands").fetchone()[0], 1)
        with self.repo.connect() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_redaction_covers_values_keys_and_url_credentials(self):
        value = Redactor([self.secret]).clean(
            {
                "nested": {"token": "hidden"},
                "message": self.secret + " https://u:p@site.example/path?k=private",
            }
        )
        output = json.dumps(value)
        for word in (self.secret, "hidden", "u:p", "private"):
            self.assertNotIn(word, output)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = Config(
            "status-bot-v2",
            root,
            {"scheduler": ("owner",)},
            ("demo",),
            {"CRITICAL": 180, "HIGH": 600, "NORMAL": 600, "BACKGROUND": 1200},
            30,
            60,
            10,
            3,
            60,
            60,
            86400,
            root / "proxy.sock",
            ("allowed.example",),
            2,
            4096,
            1,
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def dns(address):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    def test_exact_host_only(self):
        for url in (
            "https://evil.example/",
            "https://allowed.example.evil.example/",
            "https://127.0.0.1/",
        ):
            with self.subTest(url=url), self.assertRaises(PolicyDenied):
                validate_target(url, self.config.network_hosts)

    def test_credentials_http_port_fragment_rejected(self):
        for url in (
            "http://allowed.example/",
            "https://user:pass@allowed.example/",
            "https://allowed.example:8443/",
            "https://allowed.example/#x",
        ):
            with self.subTest(url=url), self.assertRaises(PolicyDenied):
                validate_target(url, self.config.network_hosts)

    def test_dns_private_metadata_loopback_ipv6_rejected(self):
        for ip in (
            "127.0.0.1",
            "169.254.169.254",
            "10.0.0.1",
            "::1",
            "fc00::1",
            "::ffff:127.0.0.1",
        ):
            with self.subTest(ip=ip), self.assertRaises(PolicyDenied):
                validate_target(
                    "https://allowed.example/",
                    self.config.network_hosts,
                    lambda *a, ip=ip, **k: self.dns(ip),
                )

    def test_mixed_public_and_private_dns_rejected(self):
        with self.assertRaises(PolicyDenied):
            validate_target(
                "https://allowed.example/",
                self.config.network_hosts,
                lambda *a, **k: self.dns("8.8.8.8") + self.dns("10.0.0.1"),
            )

    def test_dns_answer_pinned_for_tls(self):
        self.assertEqual(
            validate_target(
                "https://allowed.example/a?q=x",
                self.config.network_hosts,
                lambda *a, **k: self.dns("8.8.8.8"),
            ),
            ("allowed.example", "8.8.8.8", "/a?q=x"),
        )

    def fetch_mock(self, status=200, body=b"ok", length=None):
        response = Mock(status=status)
        response.getheader.side_effect = lambda key, default=None: (
            length if key == "Content-Length" else "text/plain"
        )
        response.read.return_value = body
        connection = Mock()
        connection.getresponse.return_value = response
        return patch(
            "studio_core.network.egress.validate_target",
            return_value=("allowed.example", "8.8.8.8", "/"),
        ), patch("studio_core.network.egress.PinnedHTTPSConnection", return_value=connection)

    def test_redirect_not_followed(self):
        target, connection = self.fetch_mock(status=302)
        with target, connection, self.assertRaisesRegex(PolicyDenied, "REDIRECT_DENIED"):
            fetch_https("https://allowed.example/", self.config)

    def test_response_size_checked_even_without_content_length(self):
        for length, body in [("999999", b"ok"), (None, b"x" * 4097)]:
            target, connection = self.fetch_mock(body=body, length=length)
            with target, connection, self.assertRaisesRegex(PolicyDenied, "RESPONSE_TOO_LARGE"):
                fetch_https("https://allowed.example/", self.config)

    def test_unix_rpc_transport_success_and_policy_error(self):
        def fixture(url, config):
            if url != "https://allowed.example/":
                raise PolicyDenied("HOST_NOT_ALLOWED")
            return {
                "status": 200,
                "content_type": "text/plain",
                "body": base64.b64encode(b"fixture").decode(),
                "sha256": hashlib.sha256(b"fixture").hexdigest(),
            }

        with EgressServer(self.config, fixture) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertEqual(
                    NetworkClient(self.config).get("https://allowed.example/").body, b"fixture"
                )
                with self.assertRaisesRegex(NetworkError, "HOST_NOT_ALLOWED"):
                    NetworkClient(self.config).get("https://evil.example/")
            finally:
                server.shutdown()
                thread.join()

    def test_timeout_is_bounded_and_redacted(self):
        def fixture(*args):
            raise TimeoutError("secret upstream URL")

        with EgressServer(self.config, fixture) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaisesRegex(NetworkError, "FETCH_FAILED"):
                    NetworkClient(self.config).get("https://allowed.example/")
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
