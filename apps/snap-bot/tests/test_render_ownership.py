import hashlib
import json
import os
import shutil
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from snap_bot.render import publish_once, render, write_once
from snap_bot.render_commit import adopt_render, observe_owned, owned_views, stage_input
from snap_bot.service import RenderHandler, render_request
from snap_bot.weather import content_hash
from studio_core.config import Config
from studio_core.jobs import LeaseLost, Repository
from studio_core.network import UnixHTTPConnection
from studio_core.storage import Storage

PHASES = ("RESEARCHING", "PROCESSING", "DOWNLOADING", "DESIGNING", "RENDERING")
ISOLATION = {"ipv4": True, "ipv6": True, "udp": True, "egress_unreachable": True}


@unittest.skipUnless(os.name == "posix", "Linux immutable spool and UID transport")
class RenderOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = replace(
            Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"),
            data_root=self.root / "data",
        )
        self.repo = Repository(self.config)
        job, _ = self.repo.enqueue(
            "render-owner", "owner", "weather", "NORMAL", "input", time.time() + 600
        )
        self.job = job["id"]
        self.owner = self.repo.claim(job_id=self.job)
        pinned = tomllib.loads(
            (Path(__file__).parents[3] / "config/snap/weather.toml").read_text(encoding="utf-8")
        )["slice"]["profile"]
        font = Path(__file__).parents[3] / pinned["font"]
        sha = pinned["font_sha256"]
        self.settings = {
            "input_root": str(self.root / "inputs/renders"),
            "spool_root": str(self.root / "spool/renders"),
            "render_socket": str(self.root / "render.sock"),
            "worker_uid": os.getuid(),
            "renderer_uid": os.getuid(),
            "egress_socket": str(self.root / "missing.sock"),
            "profile": {
                "font": str(font),
                "latin_font": str(font),
                "font_sha256": sha,
                "latin_font_sha256": sha,
                "fc_query": "fc-query",
                "ffmpeg": "ffmpeg",
                "ffprobe": "ffprobe",
                "ffmpeg_version": "ffmpeg synthetic",
                "width": 1080,
                "height": 1920,
                "fps": 24,
                "duration_seconds": 8,
                "preset": "ultrafast",
                "crf": 25,
                "timeout_seconds": 5,
                "max_bytes": 50_000_000,
            },
        }
        self.settings["profile"].update(
            {
                key: pinned[key]
                for key in (
                    "font_manifest",
                    "font_manifest_sha256",
                    "font_files",
                    "latin_font",
                    "latin_font_sha256",
                )
            }
        )
        self.settings["profile"]["font_manifest"] = str(
            Path(__file__).parents[3] / pinned["font_manifest"]
        )
        self.settings["profile"]["latin_font"] = str(
            Path(__file__).parents[3] / pinned["latin_font"]
        )
        self.content = {
            "type": "weather",
            "source": "synthetic",
            "sacred": False,
            "expires_at": time.time() + 600,
            "title": "طقس الرياض",
            "temperature_c": 29.0,
            "wind_kmh": 8.0,
            "model_time": "2026-10-03T04:00",
            "attribution": "Open-Meteo / CC BY 4.0",
        }
        self.content["content_hash"] = self.digest = content_hash(self.content)
        stage_input(self.settings, self.repo, *self.args(), self.content)
        self.video = b"synthetic owned render"
        self.render_calls = 0

    def args(self):
        return (self.job, self.owner["lease_token"], self.owner["attempt"])

    def rendering(self):
        for phase in PHASES:
            self.repo.transition(self.job, self.owner["lease_token"], phase)

    def expire(self):
        with self.repo.transaction() as db:
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, self.job))

    def fake_run(self, args, timeout):
        if "-version" in args:
            return Mock(stdout="ffmpeg synthetic\nbuild")
        if args[0] == "fc-query":
            return Mock(stdout="0-10ffff")
        if str(args[-1]).endswith(".partial.mp4"):
            self.render_calls += 1
        Path(args[-1]).write_bytes(self.video)
        return Mock(stdout="")

    def render(self, attempt=None, digest=None):
        with (
            patch("snap_bot.render.run", side_effect=self.fake_run),
            patch("snap_bot.render.proof", return_value=ISOLATION),
            patch("snap_bot.render.validate", return_value={"result": "PASS"}),
        ):
            return render(
                self.config,
                self.settings,
                self.job,
                digest or self.digest,
                attempt or self.owner["attempt"],
            )

    def adopt(self, result):
        return adopt_render(self.config, self.repo, *self.args(), result, self.settings)

    def spool(self, attempt=1):
        return Path(self.settings["spool_root"]) / self.job / "attempts" / str(attempt)

    def legacy(self):
        return Storage(self.config).workspace(self.job) / "output"

    def test_db_free_render_succeeds_with_db_permissions_000(self):
        if os.getuid() == 0:
            self.fail("This acceptance requires a non-root test process")
        self.repo.path.chmod(0)
        try:
            with self.assertRaises(PermissionError):
                self.repo.path.read_bytes()
            with patch(
                "studio_core.jobs.Repository", side_effect=AssertionError("Renderer opened DB")
            ):
                result = self.render()
            self.assertEqual(result["qa"]["result"], "PASS")
            self.assertEqual(self.render_calls, 1)
        finally:
            self.repo.path.chmod(0o600)
        self.assertIsNone(self.repo.stage_result(self.job, "design"))

    def test_render_without_any_database_or_config_data_root(self):
        cfg = SimpleNamespace(app="snap-bot-v2")
        with (
            patch("snap_bot.render.run", side_effect=self.fake_run),
            patch("snap_bot.render.proof", return_value=ISOLATION),
            patch("snap_bot.render.validate", return_value={"result": "PASS"}),
        ):
            result = render(cfg, self.settings, self.job, self.digest, 1)
        self.assertEqual(result["artifact"], f"renders/{self.job}/attempts/1/output/weather.mp4")

    def test_owner_adopts_and_records_both_stages(self):
        self.rendering()
        result = self.render()
        self.assertIsNone(self.repo.stage_result(self.job, "design"))
        receipt = self.adopt(result)
        self.assertEqual(receipt["attempt"], 1)
        self.assertEqual(self.legacy().joinpath("weather.mp4").read_bytes(), self.video)
        self.assertEqual(
            self.repo.verify_stage(
                self.job,
                "design",
                Path(self.settings["spool_root"]).parent,
                token=self.owner["lease_token"],
                attempt=1,
            )["sha256"],
            result["video_sha256"],
        )
        self.assertIsNotNone(self.repo.stage_result(self.job, "quality"))

    def test_old_attempt_input_write_does_not_change_new_attempt(self):
        old = Path(self.settings["input_root"]) / self.job / "attempts/1/input/content.json"
        self.expire()
        self.owner = self.repo.claim(job_id=self.job)
        content = {**self.content, "temperature_c": 18}
        content["content_hash"] = content_hash(content)
        current = stage_input(self.settings, self.repo, *self.args(), content)
        old.write_bytes(b"incomplete stale JSON")
        try:
            result = self.render(attempt=2, digest=content["content_hash"])
        except ValueError as error:
            self.fail("Current attempt was blocked by stale input: " + type(error).__name__)
        self.assertEqual(result["content_hash"], content["content_hash"])
        self.assertEqual(json.loads(current.read_bytes()), content)

    def test_input_create_once_does_not_replace_same_attempt(self):
        changed = {**self.content, "temperature_c": 18}
        changed["content_hash"] = content_hash(changed)
        with self.assertRaisesRegex(ValueError, "immutable"):
            stage_input(self.settings, self.repo, *self.args(), changed)
        self.assertEqual(
            json.loads(
                (
                    Path(self.settings["input_root"]) / self.job / "attempts/1/input/content.json"
                ).read_bytes()
            ),
            self.content,
        )

    def test_stale_observe_has_no_event_or_callback(self):
        args = self.args()
        self.expire()
        self.repo.claim(job_id=self.job)
        before = self.repo.timeline(self.job)
        called = []
        with self.assertRaises(LeaseLost):
            observe_owned(self.repo, *args, "design", lambda: called.append(True))
        self.assertEqual(self.repo.timeline(self.job), before)
        self.assertEqual(called, [])

    def test_observation_is_attempt_bound_and_success_is_owned(self):
        self.assertEqual(observe_owned(self.repo, *self.args(), "design", lambda: 7), 7)
        events = [
            json.loads(x["details"])
            for x in self.repo.timeline(self.job)
            if x["event"] == "agent.run"
        ]
        self.assertEqual([x["status"] for x in events], ["RUNNING", "SUCCEEDED"])
        self.assertEqual({x["attempt"] for x in events}, {1})

        def lose():
            self.expire()
            self.repo.claim(job_id=self.job)
            return 8

        with self.assertRaises(LeaseLost):
            observe_owned(self.repo, *self.args(), "quality", lose)
        events = [
            json.loads(x["details"])
            for x in self.repo.timeline(self.job)
            if x["event"] == "agent.run"
        ]
        self.assertEqual(events[-1]["status"], "RUNNING")

    def test_old_result_cannot_be_adopted_by_stale_or_new_attempt(self):
        result = self.render()
        old = self.args()
        self.expire()
        self.owner = self.repo.claim(job_id=self.job)
        with self.assertRaises(LeaseLost):
            adopt_render(self.config, self.repo, *old, result, self.settings)
        with self.assertRaises(ValueError):
            self.adopt(result)
        self.assertFalse((self.legacy() / "weather.mp4").exists())

    def test_lease_loss_after_staging_blocks_stage_and_rename(self):
        self.rendering()
        result = self.render()
        actual = os.fsync
        lost = []

        def fsync_then_lose(fd):
            actual(fd)
            if not lost:
                lost.append(True)
                self.expire()
                self.repo.claim(job_id=self.job)

        with (
            patch("snap_bot.render_commit.os.fsync", side_effect=fsync_then_lose),
            self.assertRaises(LeaseLost),
        ):
            self.adopt(result)
        self.assertIsNone(self.repo.stage_result(self.job, "design"))
        self.assertFalse((self.legacy() / "weather.mp4").exists())

    def test_crash_between_renames_retries_without_new_render(self):
        self.rendering()
        result = self.render()
        actual = os.replace
        calls = []

        def fail_second(source, target):
            calls.append(str(target))
            if len(calls) == 2:
                raise OSError("synthetic crash between renames")
            return actual(source, target)

        with (
            patch("snap_bot.render_commit.os.replace", side_effect=fail_second),
            self.assertRaises(OSError),
        ):
            self.adopt(result)
        self.assertEqual(self.render_calls, 1)
        self.assertIsNone(self.repo.stage_result(self.job, "design"))
        self.assertFalse((self.legacy() / "render-result.json").exists())
        self.adopt(result)
        self.assertEqual(self.render_calls, 1)
        self.assertEqual(json.loads((self.legacy() / "render-result.json").read_bytes()), result)
        self.assertEqual([p for p in self.legacy().iterdir() if p.name.startswith(".adopt-")], [])

    def pending_execute(self):
        from snap_bot.render_commit import AdoptionPending
        from snap_bot.service import execute

        actual = os.replace
        renames = []

        def crash_second(source, target):
            renames.append(str(target))
            if len(renames) == 2:
                raise OSError("synthetic crash between adoption renames")
            return actual(source, target)

        with (
            patch("snap_bot.service.fetch_weather", return_value=(self.content, [])),
            patch("snap_bot.service.UnixHTTPConnection") as connection,
        ):
            response = connection.return_value.getresponse.return_value

            def render_reply():
                response.status = 200
                response.read.return_value = json.dumps(self.render()).encode()
                return response

            connection.return_value.getresponse.side_effect = render_reply
            connection.return_value.sock.getsockopt.return_value = struct.pack(
                "iII", 99, os.getuid(), os.getgid()
            )
            with (
                patch("snap_bot.render_commit.os.replace", side_effect=crash_second),
                self.assertRaises(AdoptionPending),
            ):
                execute(
                    self.config,
                    self.settings,
                    "synthetic-gateway-secret-00000000000",
                    claim=self.owner,
                )
        self.assertEqual(self.repo.get(self.job)["state"], "RENDERING")
        self.assertEqual(self.repo.get(self.job)["lease_token"], self.owner["lease_token"])
        self.assertIsNone(self.repo.stage_result(self.job, "design"))
        self.assertEqual(self.render_calls, 1)

    def test_execute_adoption_fault_keeps_lease_and_explicit_retry_needs_no_render(self):
        from snap_bot.service import resume_render_adoption

        self.pending_execute()
        with (
            patch(
                "snap_bot.service.UnixHTTPConnection",
                side_effect=AssertionError("retry contacted renderer"),
            ),
            patch(
                "snap_bot.service.fetch_weather", side_effect=AssertionError("retry fetched source")
            ),
        ):
            result = resume_render_adoption(self.config, self.settings, self.owner)
        self.assertEqual(result["state"], "VALIDATING")
        self.assertEqual(self.render_calls, 1)
        self.assertEqual(self.repo.stage_result(self.job, "design")["attempt"], 1)
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)

    def test_adoption_pending_past_lease_fails_without_render_or_delivery(self):
        from snap_bot.service import resume_render_adoption
        from studio_core.repair.adoption import expire_pending_adoptions

        self.pending_execute()
        deadline = self.repo.get(self.job)["lease_until"]
        self.repo.clock = lambda: deadline + 1
        before = (self.spool() / "output/weather.mp4").read_bytes()
        with (
            patch("snap_bot.service.Repository", return_value=self.repo),
            patch(
                "snap_bot.service.UnixHTTPConnection",
                side_effect=AssertionError("expired retry rendered"),
            ),
            patch(
                "snap_bot.service.fetch_weather",
                side_effect=AssertionError("expired retry fetched"),
            ),
            self.assertRaises(LeaseLost),
        ):
            resume_render_adoption(self.config, self.settings, self.owner)
        failed = self.repo.get(self.job)
        self.assertEqual(
            (failed["state"], failed["error_code"]), ("FAILED", "ADOPTION_PENDING_EXPIRED")
        )
        self.assertIsNone(failed["lease_token"])
        self.assertIsNone(self.repo.claim(job_id=self.job))
        self.assertEqual(expire_pending_adoptions(self.repo), [])
        self.assertEqual(self.render_calls, 1)
        self.assertEqual((self.spool() / "output/weather.mp4").read_bytes(), before)
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)

    def test_pending_expiry_closes_claim_racing_preclaim_sweep(self):
        from studio_core.repair.adoption import expire_pending_adoptions

        self.pending_execute()
        deadline = self.repo.get(self.job)["lease_until"]
        self.repo.clock = lambda: deadline + 1
        newer = self.repo.claim(job_id=self.job)
        self.assertEqual(newer["recovered_from"], "RENDERING")
        self.assertEqual(expire_pending_adoptions(self.repo), [self.job])
        self.assertEqual(self.repo.get(self.job)["error_code"], "ADOPTION_PENDING_EXPIRED")
        self.assertIsNone(self.repo.claim())
        self.assertEqual(self.render_calls, 1)

    def test_missing_verified_sources_are_not_adoption_pending(self):
        from snap_bot.render_commit import AdoptionPending

        result = self.render()
        for path in (
            self.spool() / "output/weather.mp4",
            self.spool() / "output/render-result.json",
            Path(self.settings["input_root"]) / self.job / "attempts/1/input/content.json",
        ):
            data = path.read_bytes()
            path.unlink()
            try:
                with self.assertRaises(FileNotFoundError) as error:
                    self.adopt(result)
                self.assertNotIsInstance(error.exception, AdoptionPending)
            finally:
                path.write_bytes(data)
        with (
            patch("snap_bot.render_commit.tempfile.mkstemp", side_effect=OSError("staging")),
            self.assertRaises(AdoptionPending),
        ):
            self.adopt(result)

    def test_resume_diagnostics_use_explicit_claim_attempt(self):
        from snap_bot.service import resume_render_adoption

        self.pending_execute()
        timeline = self.repo.timeline(self.job)
        # Extra historical event must not shift the explicit current generation.
        extra = {"event": "job.claimed", "details": json.dumps({"attempt": 99})}
        for event in timeline:
            if (
                event["event"] == "job.state"
                and json.loads(event["details"]).get("state") == "PROCESSING"
            ):
                event["details"] = json.dumps(
                    {"state": "PROCESSING", "fallbacks": ["expected-current"]}
                )
        with (
            patch("snap_bot.service.Repository", return_value=self.repo),
            patch.object(self.repo, "timeline", return_value=[extra, *timeline]),
        ):
            self.assertEqual(
                resume_render_adoption(self.config, self.settings, self.owner)["fallbacks"],
                ["expected-current"],
            )

    def test_pending_marker_and_expiry_share_explicit_claim_attempt(self):
        from studio_core.repair.adoption import (
            claim_attempt,
            expire_pending_adoptions,
            mark_pending,
        )

        self.rendering()
        # An unexpected additional claim event must not change the explicit epoch.
        with self.repo.transaction() as db:
            self.repo.event(db, self.job, "job.claimed", {"attempt": 1, "recovered_from": None})
        mark_pending(self.repo, *self.args())
        deadline = self.repo.get(self.job)["lease_until"]
        self.repo.clock = lambda: deadline + 1
        self.assertEqual(expire_pending_adoptions(self.repo), [self.job])
        self.assertEqual(self.repo.get(self.job)["error_code"], "ADOPTION_PENDING_EXPIRED")
        self.assertEqual(claim_attempt({"attempt": 7}, 2), 7)
        self.assertEqual(claim_attempt({}, 2), 2)
        for invalid in (None, True, 0, -1, "2"):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValueError, "attempt history"),
            ):
                claim_attempt({"attempt": invalid}, 2)

    def test_directory_fsync_precedes_stage_commit_and_failure_rolls_back(self):
        self.rendering()
        result = self.render()
        actual_sync = __import__("snap_bot.render_commit", fromlist=["sync_dir"]).sync_dir
        observed = []

        def check(path):
            # Separate DB reader must see no uncommitted stage pointer.
            observed.append(self.repo.stage_result(self.job, "design"))
            actual_sync(path)

        with patch("snap_bot.render_commit.sync_dir", side_effect=check):
            self.adopt(result)
        self.assertEqual(observed, [None, None])
        with (
            patch("snap_bot.render_commit.sync_dir", side_effect=OSError("durability")),
            self.assertRaises(OSError),
        ):
            self.adopt(result)
        self.assertIsNotNone(self.repo.stage_result(self.job, "design"))

    def test_views_ownership_loss_after_validating_preserves_existing_manifest(self):
        self.rendering()
        work = Storage(self.config).workspace(self.job)
        (work / "manifest.json").write_bytes(b"new-owner-marker")
        actual = self.repo.transition

        def lose(job, token, state, details=None, *, db=None):
            actual(job, token, state, details, db=db)
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job))

        with patch.object(self.repo, "transition", side_effect=lose), self.assertRaises(LeaseLost):
            owned_views(
                self.config,
                self.repo,
                *self.args(),
                {"state": "awaiting"},
                {"state": "VALIDATING"},
                validating=True,
            )
        self.assertEqual((work / "manifest.json").read_bytes(), b"new-owner-marker")
        self.assertEqual(self.repo.get(self.job)["state"], "RENDERING")

    def test_result_declared_attempt_must_match_owned_attempt(self):
        result = self.render()
        result["attempt"] = 2
        (self.spool() / "output/render-result.json").write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
        )
        with self.assertRaisesRegex(ValueError, "bound"):
            self.adopt(result)
        self.assertIsNone(self.repo.stage_result(self.job, "design"))

    def test_tampered_spool_cannot_be_adopted(self):
        result = self.render()
        (self.spool() / "output/weather.mp4").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "hash"):
            self.adopt(result)
        self.assertIsNone(self.repo.stage_result(self.job, "design"))

    def test_all_attempt_outputs_are_immutable(self):
        result = self.render()
        files = [
            self.spool() / "output" / x
            for x in ("weather.mp4", "preview.png", "render-result.json")
        ]
        before = {p: p.read_bytes() for p in files}
        self.video = b"different bytes"
        # Retry consumes already published bytes rather than invoking another encode.
        self.assertEqual(self.render(), result)
        self.assertEqual(self.render_calls, 1)
        partial = self.spool() / "temp/overwrite.mp4"
        partial.write_bytes(self.video)
        with self.assertRaisesRegex(ValueError, "immutable"):
            publish_once(partial, self.spool() / "output/weather.mp4")
        self.assertEqual({p: p.read_bytes() for p in files}, before)

    def test_preview_and_result_refuse_changed_bytes(self):
        self.render()
        for name in ("preview.png", "render-result.json"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "immutable"):
                write_once(self.spool() / "output" / name, b"changed")

    def test_design_files_use_unique_runs_and_create_once(self):
        self.render()
        design = self.spool() / "design"
        old = {p: p.read_bytes() for p in design.rglob("*") if p.is_file()}
        self.render()
        self.assertEqual(len(list(design.iterdir())), 2)
        self.assertEqual({p: p.read_bytes() for p in old}, old)
        with self.assertRaisesRegex(ValueError, "immutable"):
            write_once(next(iter(old)), b"changed")

    def test_application_stage_schema_matches_real_core_creation(self):
        result = self.render()
        expected = f"renders/{self.job}/attempts/1/output/weather.mp4"
        self.repo.record_stage(
            self.job, self.owner["lease_token"], 1, "design", result["video_sha256"], expected
        )
        with self.repo.connect() as db:
            core = [tuple(row) for row in db.execute("PRAGMA table_info(stage_results)")]
        self.adopt(result)
        with self.repo.connect() as db:
            self.assertEqual(
                [tuple(row) for row in db.execute("PRAGMA table_info(stage_results)")], core
            )
        self.assertEqual([row[1] for row in core], ["job_id", "stage", "attempt", "sha256", "path"])

    def test_stage_insert_names_columns_when_table_order_differs(self):
        result = self.render()
        with self.repo.transaction() as db:
            db.execute(
                "CREATE TABLE stage_results(path TEXT NOT NULL,sha256 TEXT NOT NULL,attempt INTEGER NOT NULL,stage TEXT NOT NULL,job_id TEXT NOT NULL,PRIMARY KEY(job_id,stage))"
            )
        self.adopt(result)
        stage = self.repo.stage_result(self.job, "design")
        self.assertEqual(
            (stage["job_id"], stage["stage"], stage["attempt"], stage["sha256"], stage["path"]),
            (self.job, "design", 1, result["video_sha256"], result["artifact"]),
        )

    def test_extra_stage_column_refuses_without_file_adoption(self):
        result = self.render()
        with self.repo.transaction() as db:
            db.execute(
                "CREATE TABLE stage_results(job_id TEXT,stage TEXT,attempt INTEGER,sha256 TEXT,path TEXT,new_column TEXT,PRIMARY KEY(job_id,stage))"
            )
        with self.assertRaisesRegex(ValueError, "schema"):
            self.adopt(result)
        self.assertFalse((self.legacy() / "weather.mp4").exists())

    def test_late_success_event_owner_loss_keeps_old_stage_and_new_attempt_separate(self):
        result = self.render()
        old = self.args()

        def adopt_then_lose():
            self.adopt(result)
            self.expire()
            self.owner = self.repo.claim(job_id=self.job)
            return result

        with self.assertRaises(LeaseLost):
            observe_owned(self.repo, *old, "quality", adopt_then_lose)
        self.assertEqual(self.repo.get(self.job)["state"], "RUNNING")
        self.assertEqual(self.repo.stage_result(self.job, "design")["attempt"], 1)
        self.assertFalse(
            any(
                x["event"] == "job.state" and json.loads(x["details"])["state"] == "FAILED"
                for x in self.repo.timeline(self.job)
            )
        )
        with self.assertRaises(ValueError):
            self.adopt(result)
        stage_input(self.settings, self.repo, *self.args(), self.content)
        self.video = b"new attempt bytes"
        fresh = self.render(attempt=2)
        self.adopt(fresh)
        self.assertEqual(self.repo.stage_result(self.job, "design")["attempt"], 2)
        self.assertEqual(self.legacy().joinpath("weather.mp4").read_bytes(), b"new attempt bytes")
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
        events = [
            json.loads(x["details"])
            for x in self.repo.timeline(self.job)
            if x["event"] == "agent.run"
        ]
        self.assertEqual([x["status"] for x in events], ["RUNNING"])

    def test_request_has_no_lease_token_and_strict_attempt(self):
        good = {"job_id": self.job, "content_hash": self.digest, "attempt": 1}
        self.assertEqual(render_request(good), good)
        for bad in (
            {**good, "lease_token": "a" * 32},
            {**good, "attempt": True},
            {**good, "attempt": 0},
            {**good, "job_id": "../escape"},
            {k: v for k, v in good.items() if k != "attempt"},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                render_request(bad)

    def test_real_http_old_attempt_does_not_emit_renderer_telemetry(self):
        old = self.owner
        self.expire()
        self.repo.claim(job_id=self.job)
        before = self.repo.timeline(self.job)
        path = self.root / "http.sock"
        with socketserver.UnixStreamServer(str(path), RenderHandler) as server:
            server.config = self.config
            server.settings = self.settings
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            connection = UnixHTTPConnection(path, 3)
            with (
                patch("snap_bot.service.render", return_value={"stale_attempt_evidence": 1}),
                patch("snap_bot.service.observe", side_effect=AssertionError("Renderer observed")),
            ):
                connection.request(
                    "POST",
                    "/render",
                    json.dumps(
                        {"job_id": self.job, "content_hash": self.digest, "attempt": old["attempt"]}
                    ),
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                connection.close()
                thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.repo.timeline(self.job), before)

    def test_real_http_wrong_peer_never_renders(self):
        path = self.root / "http.sock"
        settings = {**self.settings, "worker_uid": os.getuid() + 1}
        with socketserver.UnixStreamServer(str(path), RenderHandler) as server:
            server.config = self.config
            server.settings = settings
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            connection = UnixHTTPConnection(path, 3)
            with patch(
                "snap_bot.service.render", return_value={"result": "unauthorized"}
            ) as callback:
                # Send header + body in one write: an unauthorized peer can be
                # rejected before the server reads the body.
                connection.connect()
                body = json.dumps(
                    {"job_id": self.job, "content_hash": self.digest, "attempt": 1}
                ).encode()
                connection.sock.sendall(
                    b"POST /render HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                connection._HTTPConnection__state = "Request-sent"
                response = connection.getresponse()
                self.assertEqual(response.status, 422)
                response.read()
                connection.close()
                thread.join(3)
                callback.assert_not_called()
            self.assertFalse(thread.is_alive())


def local_profile():
    tools = [shutil.which(name) for name in ("ffmpeg", "ffprobe", "fc-query")]
    if not all(tools):
        return None
    profile = tomllib.loads(
        (Path(__file__).parents[3] / "config/snap/weather.toml").read_text(encoding="utf-8")
    )["slice"]["profile"]
    profile = {**profile}
    for name in ("font", "latin_font", "font_manifest"):
        profile[name] = str(Path(__file__).parents[3] / profile[name])
    profile.update(
        {
            "fc_query": tools[2],
            "ffmpeg": tools[0],
            "ffprobe": tools[1],
            "ffmpeg_version": subprocess.run(
                [tools[0], "-version"], capture_output=True, text=True
            ).stdout.splitlines()[0],
            "duration_seconds": 2,
            "timeout_seconds": 120,
        }
    )
    return profile


@unittest.skipUnless(
    local_profile() or os.environ.get("MADAR_REQUIRE_REAL_RENDER") == "1",
    "Real FFmpeg tools required",
)
class RealRenderOwnershipTests(unittest.TestCase):
    def test_real_ffmpeg_db_denied_and_owned_adoption(self):
        fixture = RenderOwnershipTests()
        fixture.setUp()
        try:
            fixture.settings["profile"] = local_profile()
            if fixture.settings["profile"] is None:
                self.fail("CI requires real rendering")
            fixture.repo.path.chmod(0)
            try:
                with patch("snap_bot.render.proof", return_value=ISOLATION):
                    result = render(
                        SimpleNamespace(app="snap-bot-v2"),
                        fixture.settings,
                        fixture.job,
                        fixture.digest,
                        1,
                    )
            finally:
                fixture.repo.path.chmod(0o600)
            self.assertEqual(result["qa"]["result"], "PASS")
            fixture.rendering()
            fixture.adopt(result)
            self.assertEqual(
                hashlib.sha256((fixture.legacy() / "weather.mp4").read_bytes()).hexdigest(),
                result["video_sha256"],
            )
        finally:
            fixture.doCleanups()

    def test_real_ffmpeg_retry_after_published_video_does_not_reencode(self):
        fixture = RenderOwnershipTests()
        fixture.setUp()
        try:
            fixture.settings["profile"] = local_profile()
            if fixture.settings["profile"] is None:
                self.fail("CI requires real rendering")
            from snap_bot import render as module

            actual_write = module.write_once
            actual_run = module.run
            encodes = []

            def crash(path, data):
                if path.name == "render-result.json":
                    raise OSError("synthetic crash after video publication")
                return actual_write(path, data)

            def measured(args, timeout):
                if "lavfi" in args:
                    encodes.append(True)
                return actual_run(args, timeout)

            with (
                patch("snap_bot.render.proof", return_value=ISOLATION),
                patch("snap_bot.render.run", side_effect=measured),
            ):
                with (
                    patch("snap_bot.render.write_once", side_effect=crash),
                    self.assertRaises(OSError),
                ):
                    render(
                        SimpleNamespace(app="snap-bot-v2"),
                        fixture.settings,
                        fixture.job,
                        fixture.digest,
                        1,
                    )
                video = fixture.spool() / "output/weather.mp4"
                before = video.read_bytes()
                inode = video.stat().st_ino
                self.assertFalse((fixture.spool() / "output/render-result.json").exists())
                result = render(
                    SimpleNamespace(app="snap-bot-v2"),
                    fixture.settings,
                    fixture.job,
                    fixture.digest,
                    1,
                )
            self.assertEqual(encodes, [True])
            self.assertEqual(video.read_bytes(), before)
            self.assertEqual(video.stat().st_ino, inode)
            self.assertEqual(result["video_sha256"], hashlib.sha256(before).hexdigest())
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
