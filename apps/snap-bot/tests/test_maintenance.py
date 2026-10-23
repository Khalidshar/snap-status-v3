import gc
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from snap_bot.maintenance import (
    MAX_REPAIRS_PER_DAY,
    MAX_REPAIRS_PER_LINEAGE,
    RULE_VERSION,
    apply,
    diagnose,
)
from studio_core.config import Config
from studio_core.jobs import Repository
from studio_core.repair.adoption import mark_pending

ROOT = Path(__file__).parents[3]


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(gc.collect)
        self.now = time.time()
        self.count = 0
        config = replace(
            Config.load(ROOT / "config/snap/weather.toml"),
            data_root=Path(temp.name) / "data",
            rate_limit=100,
        )
        self.repo = Repository(config, clock=lambda: self.now)

    def failed(self, command="did_you_know", code="VERTICAL_SLICE_FAILED", actor=None):
        self.count += 1
        job, _ = self.repo.enqueue(
            f"job-{self.count}",
            actor or f"actor-{self.count}",
            command,
            "NORMAL",
            "input",
            self.now + 900,
        )
        claim = self.repo.claim(job_id=job["id"])
        self.repo.transition(job["id"], claim["lease_token"], "FAILED", {"code": code})
        return job["id"]

    def plan(self):
        return diagnose(self.repo)

    def item(self, job):
        return next(i for i in self.plan()["items"] if i.get("job_id") == job)

    def snapshot(self):
        with self.repo.connect() as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {t: {tuple(row) for row in db.execute("SELECT * FROM " + t)} for t in tables}

    def jobs(self):
        with self.repo.connect() as db:
            return [
                dict(r)
                for r in db.execute("SELECT id,state,command FROM jobs ORDER BY created_at,id")
            ]

    def test_retryable_fact_failure_becomes_one_fresh_job(self):
        job = self.failed()
        item = self.item(job)
        self.assertEqual((item["decision"], item["action"]), ("AUTO", "RETRY_FRESH_JOB"))
        report = apply(self.repo, self.plan())
        new = report["results"][0]["new_job"]
        self.assertEqual(report["results"][0]["result"], "ENQUEUED")
        self.assertEqual(self.repo.get(new)["state"], "QUEUED")
        self.assertEqual(self.repo.get(job)["state"], "FAILED")
        self.assertEqual(self.repo.get(new)["command"], "did_you_know")
        events = [e["event"] for e in self.repo.timeline(job)]
        self.assertIn("maintenance.repair_enqueued", events)
        self.assertEqual(json.loads(self.repo.timeline(new)[-1]["details"])["source_job"], job)

    def test_runner_runs_the_new_job_once_without_delivery(self):
        _job = self.failed()
        claims = []

        def runner(claim):
            claims.append(claim["id"])
            self.repo.transition(
                claim["id"], claim["lease_token"], "FAILED", {"code": "VERTICAL_SLICE_FAILED"}
            )
            return {
                "state": "VALIDATING",
                "external_delivery": False,
                "final_decision": {"delivery_decision": "BLOCKED"},
            }

        result = apply(self.repo, self.plan(), runner=runner)["results"][0]
        self.assertEqual(claims, [result["new_job"]])
        self.assertEqual((result["run"], result["final_decision"]), ("VALIDATING", "BLOCKED"))

    def test_runner_that_delivers_is_stopped(self):
        self.failed()
        with self.assertRaisesRegex(
            RuntimeError, "MAINTENANCE_RUNNER_DELIVERED", msg="guard:runner-no-delivery"
        ):
            apply(
                self.repo,
                self.plan(),
                runner=lambda claim: {"state": "COMPLETED", "external_delivery": True},
            )

    def test_runner_failure_is_recorded_not_raised(self):
        self.failed()

        def runner(claim):
            raise ValueError("render crashed")

        result = apply(self.repo, self.plan(), runner=runner)["results"][0]
        self.assertEqual(
            (result["result"], result["run"], result["error"]), ("ENQUEUED", "FAILED", "ValueError")
        )

    def test_repair_is_idempotent(self):
        job = self.failed()
        plan = self.plan()
        first = apply(self.repo, plan)
        second = apply(self.repo, plan)
        self.assertEqual(
            second["results"][0],
            {
                "job_id": job,
                "result": "ALREADY_REPAIRED",
                "new_job": first["results"][0]["new_job"],
            },
            msg="guard:repair-once",
        )
        self.assertEqual(len(self.jobs()), 2)
        self.assertNotIn(
            job,
            [i.get("job_id") for i in self.plan()["items"]],
            msg="guard:repaired-not-rediagnosed",
        )

    def test_red_line_never_resend_any_delivery_evidence(self):
        for evidence in ("deliveries", "outbox", "event", "unknown"):
            with self.subTest(evidence=evidence):
                job = self.failed(
                    code="DELIVERY_UNKNOWN" if evidence == "unknown" else "VERTICAL_SLICE_FAILED"
                )
                with self.repo.transaction() as db:
                    if evidence == "deliveries":
                        db.execute(
                            "INSERT INTO deliveries VALUES(?,?,'RESERVED')", ("d-" + job, job)
                        )
                    elif evidence == "outbox":
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS telegram_outbox(job_id TEXT,video_sha TEXT,state TEXT,message_id INTEGER)"
                        )
                        db.execute(
                            "INSERT INTO telegram_outbox VALUES(?,'x','PENDING',NULL)", (job,)
                        )
                    elif evidence == "event":
                        self.repo.event(db, job, "job.delivery_unknown", {})
                item = self.item(job)
                self.assertEqual(
                    item["decision"], "FORBIDDEN", msg="guard:never-resend-" + evidence
                )
                before = len(self.jobs())
                apply(self.repo, self.plan())
                self.assertEqual(len(self.jobs()), before)

    def test_red_line_rechecked_at_the_moment_of_action(self):
        job = self.failed()
        plan = self.plan()
        self.assertEqual(self.item(job)["decision"], "AUTO")
        with self.repo.transaction() as db:
            db.execute("INSERT INTO deliveries VALUES('late',?,'RESERVED')", (job,))
        report = apply(self.repo, plan)
        self.assertEqual(
            report["results"],
            [{"job_id": job, "result": "REFUSED_RED_LINE"}],
            msg="guard:action-time-recheck",
        )
        self.assertEqual(len(self.jobs()), 1)

    def test_automatic_delivery_commands_go_to_the_owner(self):
        job = self.failed(command="weather")
        self.assertEqual(
            (self.item(job)["decision"], self.item(job)["reason"]),
            ("OWNER", "AUTOMATIC_DELIVERY_COMMAND"),
            msg="guard:weather-never-auto",
        )
        apply(self.repo, self.plan())
        self.assertEqual(len(self.jobs()), 1)

    def test_unknown_failures_go_to_the_owner(self):
        job = self.failed(code="SOMETHING_NEW")
        self.assertEqual(self.item(job)["reason"], "UNKNOWN_FAILURE", msg="guard:unknown-code")

    def test_red_line_never_deletes_or_changes_existing_evidence(self):
        for _ in range(3):
            self.failed()
        self.failed(command="weather")
        before = self.snapshot()
        apply(self.repo, self.plan())
        after = self.snapshot()
        for table, rows in before.items():
            self.assertTrue(rows <= after[table], msg="guard:never-delete " + table)

    def test_red_line_never_writes_owner_approval(self):
        self.failed()
        report = apply(self.repo, self.plan())
        self.assertFalse(report["approves"])
        self.assertFalse(report["sends"])
        self.assertFalse(report["deletes"])
        with self.repo.connect() as db:
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='human_frame_reviews'"
                ).fetchone()
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)

    def test_lineage_budget_stops_endless_retries(self):
        job = self.failed()
        for depth in range(1, MAX_REPAIRS_PER_LINEAGE + 1):
            self.assertEqual(self.item(job)["depth"], depth)
            new = apply(self.repo, self.plan())["results"][0]["new_job"]
            claim = self.repo.claim(job_id=new)
            self.repo.transition(
                new, claim["lease_token"], "FAILED", {"code": "AGENT_CONTRACT_FAILED"}
            )
            job = new
        self.assertEqual(
            self.item(job)["reason"], "REPAIR_BUDGET_EXHAUSTED", msg="guard:lineage-budget"
        )

    def test_daily_budget_caps_repairs(self):
        jobs = [self.failed() for _ in range(MAX_REPAIRS_PER_DAY + 1)]
        decisions = [self.item(job)["decision"] for job in jobs]
        self.assertEqual(decisions.count("AUTO"), MAX_REPAIRS_PER_DAY, msg="guard:daily-budget")
        held = next(
            job for job, decision in zip(jobs, decisions, strict=False) if decision == "OWNER"
        )
        self.assertEqual(self.item(held)["reason"], "DAILY_BUDGET_EXHAUSTED")
        apply(self.repo, self.plan())
        self.assertEqual(self.item(held)["reason"], "DAILY_BUDGET_EXHAUSTED")
        self.assertEqual(len(self.jobs()), 2 * MAX_REPAIRS_PER_DAY + 1)

    def test_old_failures_are_outside_the_lookback(self):
        job = self.failed()
        self.now += 86401
        self.assertNotIn(job, [i.get("job_id") for i in self.plan()["items"]])

    def test_expired_adoption_wait_is_closed_without_reclaim(self):
        job, _ = self.repo.enqueue(
            "wait", "actor-wait", "did_you_know", "NORMAL", "input", self.now + 900
        )
        claim = self.repo.claim(job_id=job["id"])
        for state in ("RESEARCHING", "PROCESSING", "DOWNLOADING", "DESIGNING", "RENDERING"):
            self.repo.transition(job["id"], claim["lease_token"], state)
        mark_pending(self.repo, job["id"], claim["lease_token"], 1)
        self.assertNotIn("EXPIRE_ADOPTION_WAIT", [i.get("action") for i in self.plan()["items"]])
        self.now += self.repo.config.lease_seconds + 1
        self.assertIn("EXPIRE_ADOPTION_WAIT", [i.get("action") for i in self.plan()["items"]])
        report = apply(self.repo, self.plan())
        self.assertIn(
            {"job_id": job["id"], "result": "ADOPTION_WAIT_CLOSED"},
            report["results"],
            msg="guard:close-expired-wait",
        )
        self.assertEqual(self.repo.get(job["id"])["state"], "FAILED")

    def test_runtime_incidents_are_reported_and_delivery_uncertainty_forbidden(self):
        job, _ = self.repo.enqueue(
            "stuck", "actor-stuck", "weather", "NORMAL", "input", self.now + 900
        )
        claim = self.repo.claim(job_id=job["id"])
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
        self.now += self.repo.config.lease_seconds + 1
        incident = next(
            i for i in self.plan()["items"] if i.get("kind") == "JOB_DEADLINE_OR_LEASE_EXPIRED"
        )
        self.assertEqual(incident["decision"], "FORBIDDEN", msg="guard:delivery-uncertain")
        before = self.snapshot()
        apply(self.repo, self.plan())
        self.assertEqual(before, self.snapshot())

    def test_diagnosis_changes_nothing(self):
        self.failed()
        before = self.snapshot()
        plan = self.plan()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(plan["rule_version"], RULE_VERSION)

    def test_foreign_plan_is_refused(self):
        self.failed()
        for change in ({"rule_version": "old"}, {"app": "status-bot-v2"}):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(ValueError, "MAINTENANCE_PLAN_INVALID"),
            ):
                apply(self.repo, {**self.plan(), **change})
        self.assertEqual(len(self.jobs()), 1)


class MaintenanceExecuteTests(unittest.TestCase):
    def test_repair_runs_the_full_chain_and_stops_at_owner_approval(self):
        import os
        import struct
        from unittest.mock import patch

        import test_did_you_know_render as render_tests

        from snap_bot.service import execute

        fixture = render_tests.FactRendererTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        f = fixture.fixture
        with f.repo.transaction() as db:
            db.execute("UPDATE jobs SET command='did_you_know' WHERE id=?", (f.job,))
        f.repo.transition(
            f.job, f.owner["lease_token"], "FAILED", {"code": "VERTICAL_SLICE_FAILED"}
        )

        def runner(claim):
            f.job = claim["id"]
            f.owner = claim
            input_path = Path(f.settings["input_root"]) / f.job / "attempts/1/input/content.json"
            with (
                patch("snap_bot.service.NetworkClient", side_effect=AssertionError("network")),
                patch("snap_bot.service.UnixHTTPConnection") as connection,
            ):
                response = connection.return_value.getresponse.return_value

                def reply():
                    f.content = json.loads(input_path.read_bytes())
                    f.digest = f.content["content_hash"]
                    response.status = 200
                    response.read.return_value = json.dumps(fixture.fake_render()).encode()
                    return response

                connection.return_value.getresponse.side_effect = reply
                connection.return_value.sock.getsockopt.return_value = struct.pack(
                    "iII", 99, os.getuid(), os.getgid()
                )
                return execute(
                    f.config, f.settings, "synthetic-gateway-secret-at-least-32", claim=claim
                )

        failed = f.job
        result = apply(f.repo, diagnose(f.repo), runner=runner)["results"][0]
        self.assertEqual(
            (result["job_id"], result["run"], result["final_decision"]),
            (failed, "VALIDATING", "BLOCKED"),
            msg="guard:repair-full-chain",
        )
        with f.repo.connect() as db:
            roles = {
                r["role"]: r["status"]
                for r in db.execute(
                    "SELECT role,status FROM owned_agent_contracts WHERE job_id=?",
                    (result["new_job"],),
                )
            }
            self.assertEqual(db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
        self.assertEqual(roles["judge"], "SUCCEEDED")
        self.assertEqual(roles["final"], "SUCCEEDED")
        self.assertEqual(f.repo.get(failed)["state"], "FAILED")


if __name__ == "__main__":
    unittest.main()
