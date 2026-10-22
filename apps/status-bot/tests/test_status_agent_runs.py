"""Actual Status adapters and local artifact binding, no external transport."""

import copy
import hashlib
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_status_weather as fixture_weather

from status_bot.pipeline import prepare, record_validation
from status_bot.render import render
from status_bot.weather import SourceInvalid, content_hash
from studio_core.jobs import LeaseLost, Repository
from studio_core.jobs.component_lock import ComponentBusy, ComponentLocks
from studio_core.observability.agent_runs import AgentRejected
from studio_core.observability.watchdog import inspect_runtime
from studio_core.storage import Storage

FixtureClient = fixture_weather.FixtureClient


class StatusAgentRuns(unittest.TestCase):
    # Reuse configuration helpers without duplicating the older test cases.
    def setUp(self):
        fixture_weather.StatusWeather.setUp(self)

    def fixture(self):
        prepared = prepare(
            self.cfg, self.settings, FixtureClient(), self.secret, "agent_1", synthetic=True
        )
        root = Storage(self.cfg).workspace(prepared["claim"]["id"])
        data = b"unit-fixture-not-real-video"
        (root / "output/weather.mp4").write_bytes(data)
        result = {
            "job_id": prepared["claim"]["id"],
            "content_hash": prepared["content"]["content_hash"],
            "video_sha256": hashlib.sha256(data).hexdigest(),
            "qa": {"result": "PASS"},
        }
        (root / "output/render-result.json").write_text(json.dumps(result))
        return prepared, root, result

    def test_three_actual_owned_roles_and_no_acceptance_inference(self):
        prepared, root, result = self.fixture()
        record_validation(self.cfg, prepared, result)
        repo = Repository(self.cfg)
        with repo.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM agent_runs ORDER BY started_at")]
        self.assertEqual([r["role"] for r in rows], ["fact_source", "arabic", "quality"])
        self.assertTrue(
            all(
                r["status"] == "SUCCEEDED" and r["app"] == "status-bot-v2" and r["attempt"] == 1
                for r in rows
            )
        )
        self.assertEqual(rows[0]["output_sha"], rows[1]["input_sha"])
        observed = inspect_runtime(repo.path, self.cfg.app)
        self.assertTrue(all(r["function_acceptance"] == "NOT_VERIFIED" for r in observed["agents"]))
        self.assertFalse(observed["delivery_authority"])

    def test_quality_rejects_missing_changed_or_linked_video(self):
        self.cfg = replace(self.cfg, rate_limit=10)
        for change in ("missing", "changed", "symlink"):
            with self.subTest(change=change):
                # Each failure keeps its isolated job and does not replay QA.
                event = "qa_" + change
                prepared = prepare(
                    self.cfg, self.settings, FixtureClient(), self.secret, event, synthetic=True
                )
                root = Storage(self.cfg).workspace(prepared["claim"]["id"])
                data = b"video-unit-fixture"
                video = root / "output/weather.mp4"
                result = {
                    "job_id": prepared["claim"]["id"],
                    "content_hash": prepared["content"]["content_hash"],
                    "video_sha256": hashlib.sha256(data).hexdigest(),
                    "qa": {"result": "PASS"},
                }
                if change == "changed":
                    video.write_bytes(b"tampered")
                if change == "symlink":
                    target = root / "temp/linked.mp4"
                    target.write_bytes(data)
                    video.symlink_to(target)
                with self.assertRaises(AgentRejected):
                    record_validation(self.cfg, prepared, result)
                repo = Repository(self.cfg)
                repo.transition(prepared["claim"]["id"], prepared["claim"]["lease_token"], "FAILED")

    def test_changed_stored_input_cannot_be_accepted_by_hash_of_original(self):
        prepared, root, result = self.fixture()
        source = copy.deepcopy(prepared["content"])
        source["title"] = "طقس آخر"
        source["content_hash"] = content_hash(source)
        (root / "input/content.json").write_text(json.dumps(source))
        with self.assertRaises(AgentRejected):
            record_validation(self.cfg, prepared, result)
        self.assertEqual(Repository(self.cfg).get(prepared["claim"]["id"])["state"], "RENDERING")

    def test_changed_prepared_content_cannot_forge_source_lineage(self):
        prepared, root, result = self.fixture()
        prepared["content"]["title"] = "تغيير العنوان"
        prepared["content"]["content_hash"] = content_hash(prepared["content"])
        result["content_hash"] = prepared["content"]["content_hash"]
        (root / "input/content.json").write_text(json.dumps(prepared["content"]))
        with self.assertRaises(AgentRejected):
            record_validation(self.cfg, prepared, result)

    def test_changed_qa_metadata_cannot_replace_the_renderers_record(self):
        prepared, root, result = self.fixture()
        result["qa"]["claimed_extra_check"] = "PASS"
        with self.assertRaises(AgentRejected):
            record_validation(self.cfg, prepared, result)
        self.assertEqual(Repository(self.cfg).get(prepared["claim"]["id"])["state"], "RENDERING")

    def test_maintenance_excludes_real_renderer_before_tools(self):
        prepared, root, result = self.fixture()
        locks = ComponentLocks(Repository(self.cfg))
        with locks.hold("weather-render", "MAINTENANCE"), patch("status_bot.render.proof") as proof:
            with self.assertRaises(ComponentBusy):
                render(self.cfg, {}, prepared["claim"]["id"], result["content_hash"])
            proof.assert_not_called()

    def test_repair_excludes_quality_before_transition(self):
        prepared, root, result = self.fixture()
        locks = ComponentLocks(Repository(self.cfg))
        with locks.hold("weather-render", "REPAIR"):
            with self.assertRaises(ComponentBusy):
                record_validation(self.cfg, prepared, result)
        self.assertEqual(Repository(self.cfg).get(prepared["claim"]["id"])["state"], "RENDERING")

    def test_stale_attempt_cannot_commit_manifest(self):
        prepared, root, result = self.fixture()
        repo = Repository(self.cfg)
        with repo.transaction() as db:
            db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (prepared["claim"]["id"],))
        next_claim = repo.claim(prepared["claim"]["id"])
        self.assertEqual(next_claim["attempt"], 2)
        with self.assertRaises(LeaseLost):
            record_validation(self.cfg, prepared, result)
        self.assertFalse((root / "manifest.json").exists())

    def test_queue_preparation_claims_only_new_request(self):
        cfg = replace(self.cfg, max_active_per_actor=2)
        repo = Repository(cfg)
        first, _ = repo.enqueue(
            "older-job", "other-owner", "weather", "HIGH", "older-hash", 9999999999
        )
        prepared = prepare(
            cfg, self.settings, FixtureClient(), self.secret, "new_request", synthetic=True
        )
        self.assertNotEqual(first["id"], prepared["claim"]["id"])
        self.assertEqual(repo.get(first["id"])["state"], "QUEUED")

    def test_failed_source_keeps_failed_role_and_releases_input_lock(self):
        with patch(
            "status_bot.pipeline.fetch_weather", side_effect=SourceInvalid("fixture-source-failure")
        ):
            with self.assertRaises(SourceInvalid):
                prepare(self.cfg, self.settings, FixtureClient(), self.secret, "failed_1")
        repo = Repository(self.cfg)
        with repo.connect() as db:
            self.assertEqual(
                db.execute("SELECT status FROM agent_runs WHERE role='fact_source'").fetchone()[0],
                "FAILED",
            )
            self.assertEqual(db.execute("SELECT count(*) FROM component_locks").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM jobs").fetchone()[0], "FAILED")

    def test_non_arabic_or_bidi_labels_fail_before_file_write(self):
        from status_bot.agents import weather_payload

        prepared, root, result = self.fixture()
        for title in ("English-only title", "طقس\u202eآخر"):
            value = copy.deepcopy(prepared["source_payload"])
            value["content"]["title"] = title
            value["content"]["content_hash"] = content_hash(value["content"])
            self.assertFalse(weather_payload(value))


class RenderFencing(unittest.TestCase):
    def setUp(self):
        fixture_weather.StatusWeather.setUp(self)

    def test_stale_renderer_cannot_commit_any_final_output(self):
        from pathlib import Path
        from types import SimpleNamespace

        prepared = prepare(
            self.cfg, self.settings, FixtureClient(), self.secret, "render_epoch", synthetic=True
        )
        job = prepared["claim"]["id"]
        root = Storage(self.cfg).workspace(job)
        font = self.root / "fixture-font"
        font.write_bytes(b"fixture-font-bytes")
        profile = {
            "font": str(font),
            "latin_font": str(font),
            "font_sha256": hashlib.sha256(font.read_bytes()).hexdigest(),
            "latin_font_sha256": hashlib.sha256(font.read_bytes()).hexdigest(),
            "fc_query": "fc-fixture",
            "ffmpeg": "ffmpeg-fixture",
            "ffmpeg_version": "fixture-version",
            "width": 1080,
            "height": 1920,
            "fps": 24,
            "duration_seconds": 8,
            "timeout_seconds": 30,
            "preset": "ultrafast",
            "crf": 25,
        }

        def fake_run(args, timeout):
            if args[0] == "fc-fixture":
                return SimpleNamespace(stdout="20-10ffff")
            if "-version" in args:
                return SimpleNamespace(stdout="fixture-version\n")
            if str(args[-1]).endswith("weather.partial.mp4"):
                Path(args[-1]).write_bytes(b"partial-unit-video")
            if str(args[-1]).endswith("preview.partial.png"):
                Path(args[-1]).write_bytes(b"partial-unit-preview")
                repo = Repository(self.cfg)
                with repo.transaction() as db:
                    db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job,))
                repo.claim(job)
            return SimpleNamespace(stdout="")

        with (
            patch("status_bot.render.proof", return_value={"fixture": True}),
            patch("status_bot.render.run", side_effect=fake_run),
            patch("status_bot.render.validate", return_value={"result": "PASS"}),
        ):
            with self.assertRaises(LeaseLost):
                render(
                    self.cfg,
                    {"egress_socket": "fixture", "profile": profile},
                    job,
                    prepared["content"]["content_hash"],
                )
        self.assertFalse((root / "output/weather.mp4").exists())
        self.assertFalse((root / "output/preview.png").exists())
        self.assertFalse((root / "output/render-result.json").exists())
        self.assertTrue((root / "temp/weather.partial.mp4").exists())
        self.assertEqual(Repository(self.cfg).get(job)["state"], "RUNNING")

    def test_terminal_job_cannot_be_rendered_again(self):
        prepared = prepare(
            self.cfg, self.settings, FixtureClient(), self.secret, "terminal", synthetic=True
        )
        claim = prepared["claim"]
        repo = Repository(self.cfg)
        repo.transition(claim["id"], claim["lease_token"], "FAILED")
        with patch("status_bot.render.proof") as proof:
            with self.assertRaises(ValueError):
                render(self.cfg, {}, claim["id"], prepared["content"]["content_hash"])
            proof.assert_not_called()


if __name__ == "__main__":
    unittest.main()
