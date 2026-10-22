"""Status consumes the pinned artifact; it owns no copy of the shared implementation."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from studio_core import __version__
from studio_core.config import Config
from studio_core.jobs import Conflict, Repository
from studio_core.jobs.gateway import CommandGateway, Unauthorized
from studio_core.storage import Storage


class StatusContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = replace(
            Config.load(Path(__file__).parents[3] / "config" / "status" / "foundation.toml"),
            data_root=Path(self.temp.name),
        )
        self.repo = Repository(self.config)
        self.gateway = CommandGateway(
            self.config, self.repo, "contract-test-only-not-a-production-key" * 2
        )

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, **extra):
        import time

        body = {
            "channel": "scheduler",
            "actor": "foundation-test-owner",
            "event_id": "status-contract",
            "command": "demo",
            **extra,
        }
        now = time.time()
        return self.gateway.accept(body, now, self.gateway.signature(body, now))

    def test_pinned_version_and_separate_status_database(self):
        import studio_core

        self.assertTrue(__version__) and self.assertIn("packages/studio-core", studio_core.__file__)
        job, _ = self.submit()
        self.assertEqual(job["app"], "status-bot-v2")
        self.assertEqual(self.repo.path.parent, Path(self.temp.name))

    def test_gateway_authorization_and_idempotency(self):
        with self.assertRaises(Unauthorized):
            self.submit(actor="other")
        first, _ = self.submit()
        second, created = self.submit()
        self.assertFalse(created)
        self.assertEqual(first["id"], second["id"])
        with self.assertRaises(Conflict):
            self.submit(priority="CRITICAL")

    def test_pipeline_and_retained_manifest(self):
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
            "COMPLETED",
        ):
            self.repo.transition(job["id"], claim["lease_token"], state)
        storage = Storage(self.config)
        storage.manifest(job["id"], {"app": "status-bot-v2", "external_delivery": False})
        self.assertEqual(storage.gc(self.repo), [])
        self.assertTrue((storage.metadata / (job["id"] + ".json")).exists())

    def test_renderer_primitives_come_from_the_pinned_core(self):
        import status_bot.render as render
        import studio_core.renderer as core

        for name in ("run", "ensure_glyphs", "proof"):
            self.assertIs(getattr(render, name), getattr(core, name), "guard:no-local-copy " + name)
        source = Path(render.__file__).read_text(encoding="utf-8")
        self.assertNotIn("def run(", source, "guard:no-local-copy")
        self.assertNotIn("def proof(", source, "guard:no-local-copy")

    def test_delivery_evidence_comes_from_the_core_and_keeps_status_shape(self):
        import status_bot.telegram_egress as egress
        import studio_core.delivery as core

        for name in ("DeliveryConfirmed", "atomic_json", "receipt_metadata", "remember_review"):
            self.assertIs(
                getattr(egress, name), getattr(core, name), "guard:no-local-delivery-copy " + name
            )
        source = Path(egress.__file__).read_text(encoding="utf-8")
        self.assertNotIn("def atomic_json(", source, "guard:no-local-delivery-copy")
        # Status records the terminal state in its manifest when repairing evidence.
        self.assertIn(
            "manifest_state='COMPLETED'", source.replace('"', "'"), "guard:status-manifest-state"
        )


if __name__ == "__main__":
    unittest.main()
