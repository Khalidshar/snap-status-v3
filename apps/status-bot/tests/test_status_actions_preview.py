import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from status_bot import actions_preview as actions
from status_bot.pipeline import prepare, record_validation
from status_bot.telegram_egress import EXPECTED_BOT
from studio_core.jobs import Repository
from studio_core.storage import Storage

sys.path.insert(0, str(Path(__file__).parent))
from test_status_weather import FixtureClient

ROOT = Path(__file__).parents[3]
ISOLATION = {"ipv4": True, "ipv6": True, "udp": True, "egress_unreachable": True}


def environment():
    return {
        "GITHUB_REPOSITORY": actions.REPOSITORY,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_ACTOR": "Khalidshar",
        "GITHUB_TRIGGERING_ACTOR": "Khalidshar",
        "HUMAN_APPROVAL": "HUMAN_FRAME",
        "VIDEO_SHA": "a" * 64,
        "CONTENT_HASH": "b" * 64,
        "SOURCE_RUN": "7",
        "GITHUB_RUN_ID": "8",
        "GITHUB_SHA": "c" * 40,
        "SEND_INTENT_ARTIFACT_ID": "9",
    }


def blocked(call):
    try:
        call()
    except ValueError:
        return True
    return False


class GuardTests(unittest.TestCase):
    def setUp(self):
        policy = patch("status_bot.platform_policy.reject_status_telegram", new=lambda: None)
        policy.start()
        self.addCleanup(policy.stop)

    def test_authority_and_explicit_approval_are_required(self):
        env = environment()
        actions.approval_guard(env)
        for key, value in [
            ("GITHUB_REPOSITORY", "Khalidshar/snap-bot-v2"),
            ("GITHUB_EVENT_NAME", "push"),
            ("GITHUB_REF", "refs/heads/x"),
            ("GITHUB_ACTOR", "other"),
            ("GITHUB_TRIGGERING_ACTOR", "other"),
            ("HUMAN_APPROVAL", "ASSISTANT_FRAME"),
            ("VIDEO_SHA", "A" * 64),
            ("CONTENT_HASH", ""),
            ("SOURCE_RUN", "01"),
        ]:
            with self.subTest(key=key):
                self.assertTrue(
                    blocked(lambda: actions.approval_guard({**env, key: value})),
                    "guard:authority-" + key,
                )

    def test_invalid_approval_never_reaches_github(self):
        calls = []
        env = {**environment(), "HUMAN_APPROVAL": ""}
        self.assertTrue(
            blocked(lambda: actions.preflight(env, lambda *a: calls.append(a))),
            "guard:preflight-approval",
        )
        self.assertEqual(calls, [], "guard:no-github-before-approval")

    def test_source_is_manual_successful_same_commit_recent(self):
        env = environment()
        run = {
            "id": 7,
            "event": "workflow_dispatch",
            "path": actions.RENDER_WORKFLOW,
            "conclusion": "success",
            "head_sha": env["GITHUB_SHA"],
            "repository": {"full_name": actions.REPOSITORY},
            "created_at": datetime.fromtimestamp(1000, UTC).isoformat(),
        }
        actions.source_guard(run, env, 1100)
        for key, value in [
            ("event", "push"),
            ("path", ".github/workflows/foundation.yml"),
            ("conclusion", "failure"),
            ("head_sha", "d" * 40),
            ("id", 6),
            ("repository", {"full_name": "other/repo"}),
        ]:
            with self.subTest(key=key):
                self.assertTrue(
                    blocked(lambda: actions.source_guard({**run, key: value}, env, 1100)),
                    "guard:source-" + key,
                )
        self.assertTrue(
            blocked(lambda: actions.source_guard(run, env, 1000 + actions.BUNDLE_SECONDS)),
            "guard:source-expiry",
        )

    def test_existing_intent_blocks_even_expired(self):
        for expired in (False, True):
            self.assertTrue(
                blocked(
                    lambda: actions.history_guard(
                        [{"name": actions.INTENT_PREFIX + "a" * 64, "expired": expired}], "a" * 64
                    )
                ),
                "guard:no-repeat-intent",
            )
        actions.history_guard([{"name": actions.INTENT_PREFIX + "b" * 64}], "a" * 64)

    def test_persistent_intent_matches_current_run_and_video(self):
        env = environment()
        artifact = {
            "id": 9,
            "name": actions.INTENT_PREFIX + env["VIDEO_SHA"],
            "expired": False,
            "workflow_run": {"id": 8},
        }
        actions.intent_guard(env, artifact)
        for changed in (
            {**artifact, "id": 10},
            {**artifact, "name": "other"},
            {**artifact, "expired": True},
            {**artifact, "workflow_run": {"id": 7}},
        ):
            self.assertTrue(
                blocked(lambda: actions.intent_guard(env, changed)), "guard:durable-intent"
            )

    # The lab's GitHub Actions workflows are not part of v3 (GRADUATION.md: actions path not moved).


class BundleTests(unittest.TestCase):
    """Bundle shaped exactly like tools/check_weather_render.py --bundle output."""

    def setUp(self):
        # Exercise historical bundle guards without authorizing a real transport.
        policy = patch("status_bot.platform_policy.reject_status_telegram", new=lambda: None)
        policy.start()
        self.addCleanup(policy.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name) / "bundle"
        config = actions.bundle_config(self.bundle)
        settings = {
            "city": "الرياض",
            "ttl_seconds": 3600,
            "sources": [
                {
                    "id": "open-meteo",
                    "url": "https://api.open-meteo.com/fixture",
                    "license": "fixture",
                    "attribution": "Fixture",
                }
            ],
        }
        prepared = prepare(
            config,
            settings,
            FixtureClient(),
            "status-actions-fixture-gateway-secret",
            "actions_1",
            synthetic=True,
        )
        self.job = prepared["claim"]["id"]
        root = Storage(config).workspace(self.job)
        self.video = b"reviewed synthetic weather bytes"
        (root / "output/weather.mp4").write_bytes(self.video)
        (root / "output/preview.png").write_bytes(b"preview")
        result = {
            "job_id": self.job,
            "content_hash": prepared["content"]["content_hash"],
            "video_sha256": hashlib.sha256(self.video).hexdigest(),
            "qa": {"result": "PASS"},
            "network_isolation": ISOLATION,
            "glyph_coverage": "PASS",
        }
        (root / "output/render-result.json").write_text(json.dumps(result))
        record_validation(config, prepared, result)
        for name in ("weather.mp4", "preview.png"):
            shutil.copyfile(root / "output" / name, self.bundle / name)
        self.env = {
            **environment(),
            "VIDEO_SHA": result["video_sha256"],
            "CONTENT_HASH": result["content_hash"],
        }
        self.metadata = {
            "schema": 1,
            "repository": actions.REPOSITORY,
            "run_id": "7",
            "head_sha": "c" * 40,
            "job_id": self.job,
            "lease_token": prepared["claim"]["lease_token"],
            "render": result,
            "content_hash": result["content_hash"],
            "video_sha256": result["video_sha256"],
            "preview_sha256": hashlib.sha256(b"preview").hexdigest(),
            "expires_at": prepared["content"]["expires_at"],
            "synthetic_source": True,
            "delivery": "NOT_ATTEMPTED",
            "human_approval": None,
        }
        self.write_metadata()
        self.calls = []

    def write_metadata(self, **changes):
        (self.bundle / "preview.json").write_text(json.dumps({**self.metadata, **changes}))

    def transport(self, token, method, body=b"", kind="application/json"):
        self.calls.append(method)
        if method == "getMe":
            return {"username": EXPECTED_BOT}
        if method == "getChat":
            return {"id": 42, "type": "private"}
        return {"message_id": 77, "chat": {"id": 42}}

    def github(self, path, env):
        return {
            "id": 9,
            "name": actions.INTENT_PREFIX + self.env["VIDEO_SHA"],
            "expired": False,
            "workflow_run": {"id": 8},
        }

    def test_valid_bundle_verifies(self):
        config, data = actions.verify_bundle(self.bundle, self.env)
        self.assertEqual(data["job_id"], self.job)
        self.assertEqual(
            Repository(config).get(self.job)["state"], "VALIDATING", "guard:awaiting-review"
        )

    def test_tampering_is_rejected(self):
        cases = {
            "provenance": lambda: self.write_metadata(head_sha="d" * 40),
            "approved-before": lambda: self.write_metadata(human_approval="HUMAN_FRAME"),
            "expired": lambda: self.write_metadata(expires_at=time.time() - 1),
            "preview": lambda: (self.bundle / "preview.png").write_bytes(b"other"),
            "video": lambda: (self.bundle / "weather.mp4").write_bytes(b"other"),
            "render": lambda: self.write_metadata(
                render={**self.metadata["render"], "glyph_coverage": "FAIL"}
            ),
        }
        for name, tamper in cases.items():
            with self.subTest(name=name):
                self.setUp()
                tamper()
                with self.assertRaises(Exception, msg="guard:bundle-" + name):
                    actions.verify_bundle(self.bundle, self.env)

    def test_wrong_reviewed_digest_rejected(self):
        for key in ("VIDEO_SHA", "CONTENT_HASH"):
            with self.subTest(key=key):
                self.assertTrue(
                    blocked(
                        lambda: actions.verify_bundle(self.bundle, {**self.env, key: "0" * 64})
                    ),
                    "guard:exact-" + key,
                )

    def test_one_send_then_no_replay(self):
        credentials = {"STATUS_TG_TEST_TOKEN": "123456:" + "x" * 35, "STATUS_TG_TEST_CHAT": "42"}
        with patch.dict(os.environ, credentials):
            proof = actions.send_bundle(self.bundle, self.env, self.github, self.transport)
            self.assertEqual(proof["receipt"]["delivery"], "PUBLISHED", "guard:published")
            self.assertEqual(proof["receipt"]["message_id"], 77)
            self.assertEqual(self.calls.count("sendVideo"), 1)
            confirmed_receipt = (self.bundle / "receipt.json").read_bytes()
            with self.assertRaises(Exception, msg="guard:no-resend-after-ack"):
                actions.send_bundle(self.bundle, self.env, self.github, self.transport)
        self.assertEqual(self.calls.count("sendVideo"), 1, "guard:single-send")
        receipt = json.loads((self.bundle / "receipt.json").read_text())
        self.assertEqual(receipt["receipt"]["delivery"], "PUBLISHED", "guard:never-downgrade-ack")
        self.assertEqual((self.bundle / "receipt.json").read_bytes(), confirmed_receipt)
        self.assertNotIn(
            "x" * 35, (self.bundle / "receipt.json").read_text(), "guard:no-token-in-receipt"
        )

    def test_wrong_intent_blocks_before_telegram(self):
        with (
            patch.dict(
                os.environ,
                {"STATUS_TG_TEST_TOKEN": "123456:" + "x" * 35, "STATUS_TG_TEST_CHAT": "42"},
            ),
            self.assertRaises(ValueError),
        ):
            actions.send_bundle(
                self.bundle,
                self.env,
                lambda p, e: {**self.github(p, e), "workflow_run": {"id": 1}},
                self.transport,
            )
        self.assertEqual(self.calls, [], "guard:intent-before-network")


if __name__ == "__main__":
    unittest.main()
