import copy
import gc
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from snap_bot.agent_contract_map import (
    contract_plan,
    project_events,
    read_snapshot,
    validate_payload,
)
from snap_bot.did_you_know import load_facts, make_content
from studio_core.config import Config
from studio_core.jobs import LeaseLost, Repository

ROOT = Path(__file__).parents[3]
REGISTRY = json.loads(
    (ROOT / "apps/snap-bot/snap_bot/madar/registry.json").read_text(encoding="utf-8")
)
JOB = "a" * 32


def run_events(role, run, *, status="SUCCEEDED", attempt=1):
    base = {"agent": role, "attempt": attempt, "run": run * 32, "duration_ms": 2}
    return [
        {"at": 10, "event": "agent.run", "details": json.dumps({**base, "status": "RUNNING"})},
        {"at": 11, "event": "agent.run", "details": json.dumps({**base, "status": status})},
    ]


# The four check agents that run between arabic and design, in chain order.
CHECK_EVENTS = [
    event
    for role, run in (
        ("memory", "b"),
        ("rotation", "c"),
        ("proofreading", "d"),
        ("translation", "e"),
    )
    for event in run_events(role, run)
]
# The three render agents that run between design and quality, in chain order.
RENDER_EVENTS = [
    event
    for role, run in (("voice", "f"), ("media_verification", "6"), ("judge", "7"))
    for event in run_events(role, run)
]


class AgentContractMapTests(unittest.TestCase):
    def view(self, events, attempt=1, now=20):
        return project_events(JOB, attempt, events, now=now, registry=REGISTRY)

    def roles(self, events, **kwargs):
        return {r["id"]: r for r in self.view(events, **kwargs)["roles"]}

    def render_result(self):
        return {
            "job_id": JOB,
            "attempt": 1,
            "content_hash": "b" * 64,
            "video_sha256": "c" * 64,
            "artifact": f"renders/{JOB}/attempts/1/output/did-you-know.mp4",
            "qa": {"result": "PASS"},
            "content_type": "did_you_know",
        }

    def test_eighteen_roles_stay_unaccepted_and_unbound_are_explicit(self):
        plan = contract_plan(REGISTRY)
        self.assertEqual(len(plan["roles"]), 18)
        self.assertEqual(sum(r["binding"] == "EXISTING_OBSERVER" for r in plan["roles"]), 12)
        self.assertEqual(
            {r["id"] for r in plan["roles"] if r["binding"] == "EXISTING_OBSERVER"},
            {
                "fact_source",
                "arabic",
                "memory",
                "rotation",
                "proofreading",
                "translation",
                "design",
                "voice",
                "media_verification",
                "judge",
                "quality",
                "final",
            },
        )
        self.assertTrue(
            all(
                r["adapter_status"] == "NOT_IMPLEMENTED"
                and r["acceptance_status"] == "NOT_VERIFIED"
                for r in plan["roles"]
            )
        )
        self.assertTrue(all(r["input_contract"] and r["output_contract"] for r in plan["roles"]))
        self.assertFalse(plan["delivery_authority"])
        self.assertFalse(plan["automatic_repair"])
        self.assertEqual(self.roles([])["writer"]["status"], "LOCAL_TOOL")
        tools = {r["id"] for r in plan["roles"] if r["binding"] == "LOCAL_TOOL"}
        self.assertEqual(
            tools, {"manager", "analyst", "writer", "reviewer", "fixer", "content_repair"}
        )
        self.assertFalse(
            [r["id"] for r in plan["roles"] if r["binding"] == "NOT_BOUND"],
            msg="guard:all-eighteen-bound",
        )

    def test_missing_duplicate_and_wrong_application_registry_rejected(self):
        for change in ("missing", "duplicate", "app"):
            bad = copy.deepcopy(REGISTRY)
            if change == "missing":
                bad["agents"].pop()
            elif change == "duplicate":
                bad["agents"][0]["id"] = bad["agents"][1]["id"]
            else:
                bad["agents"][0]["applications"] = ["status-bot-v2"]
            with self.subTest(change=change), self.assertRaises(ValueError):
                contract_plan(bad)

    def test_missing_inputs_rejected_and_valid_catalog_content_accepted(self):
        content = make_content(load_facts()[0], 10)
        self.assertTrue(validate_payload("fact_source", "input", {"command": "did_you_know"}))
        self.assertTrue(validate_payload("fact_source", "output", content))
        self.assertTrue(validate_payload("design", "input", content))
        for role, direction in (
            ("fact_source", "input"),
            ("fact_source", "output"),
            ("design", "input"),
            ("design", "output"),
            ("quality", "input"),
            ("quality", "output"),
        ):
            with self.subTest(role=role, direction=direction), self.assertRaises(ValueError):
                validate_payload(role, direction, {})
        for role in ("voice", "media_verification", "judge", "writer"):
            with self.subTest(role=role), self.assertRaises(ValueError):
                validate_payload(role, "input", {})

    def test_render_contract_rejects_missing_qa_wrong_attempt_and_digest(self):
        value = self.render_result()
        self.assertTrue(validate_payload("design", "output", value))
        for changes in (
            {"qa": None},
            {"qa": {"result": "FAIL"}},
            {"attempt": 2},
            {"content_hash": "bad"},
            {"job_id": "../escape"},
            {"content_type": "weather"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_payload("quality", "input", {**value, **changes})

    def test_receipt_matches_attempt_path_and_explicit_columns(self):
        receipt = {
            "job_id": JOB,
            "stage": "design",
            "attempt": 1,
            "sha256": "b" * 64,
            "path": self.render_result()["artifact"],
        }
        self.assertTrue(validate_payload("quality", "output", receipt))
        for changes in (
            {"path": "renders/other.mp4"},
            {"stage": "quality"},
            {"attempt": 2},
            {"sha256": "bad"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_payload("quality", "output", {**receipt, **changes})

    def test_failure_groups_dependents_under_one_incident(self):
        roles = self.roles(run_events("fact_source", "1", status="FAILED"))
        self.assertEqual(roles["fact_source"]["status"], "FAILED")
        for role in ("design", "quality"):
            self.assertEqual(roles[role]["status"], "BLOCKED_BY_UPSTREAM")
            self.assertEqual(roles[role]["root_role"], "fact_source")
            self.assertEqual(roles[role]["incident"], roles["fact_source"]["incident"])
        self.assertEqual(roles["analyst"]["status"], "LOCAL_TOOL")

    def test_quality_before_judge_is_an_invalid_sequence(self):
        events = (
            run_events("fact_source", "1")
            + run_events("arabic", "a")
            + CHECK_EVENTS
            + run_events("design", "2")
            + RENDER_EVENTS[:4]
            + run_events("quality", "3")
        )
        with self.assertRaisesRegex(ValueError, "before successful dependency"):
            self.view(events)

    def test_render_check_failure_blocks_quality_under_its_incident(self):
        roles = self.roles(
            run_events("fact_source", "1")
            + run_events("arabic", "a")
            + CHECK_EVENTS
            + run_events("design", "2")
            + run_events("voice", "f", status="FAILED")
        )
        self.assertEqual(roles["design"]["status"], "SUCCEEDED")
        for role in ("media_verification", "judge", "quality"):
            self.assertEqual(
                (roles[role]["status"], roles[role]["root_role"]), ("BLOCKED_BY_UPSTREAM", "voice")
            )

    def test_design_failure_keeps_successful_source(self):
        roles = self.roles(
            run_events("fact_source", "1")
            + run_events("arabic", "a")
            + CHECK_EVENTS
            + run_events("design", "2", status="FAILED")
        )
        self.assertEqual(roles["fact_source"]["status"], "SUCCEEDED")
        self.assertEqual(roles["quality"]["root_role"], "design")

    def test_old_attempt_events_do_not_leak_success_or_failure(self):
        roles = self.roles(run_events("fact_source", "1", status="FAILED"), attempt=2)
        self.assertTrue(
            all(roles[r]["status"] == "NOT_OBSERVED" for r in ("fact_source", "design", "quality"))
        )

    def test_running_becomes_unknown_when_stale(self):
        event = run_events("fact_source", "1")[:1]
        self.assertEqual(self.roles(event, now=130)["fact_source"]["status"], "RUNNING")
        self.assertEqual(self.roles(event, now=131)["fact_source"]["status"], "UNKNOWN_STALE")
        self.assertEqual(self.roles([])["fact_source"]["status"], "NOT_OBSERVED")

    def test_success_never_claims_outputs_verified(self):
        roles = self.roles(
            run_events("fact_source", "1")
            + run_events("arabic", "a")
            + CHECK_EVENTS
            + run_events("design", "2")
            + RENDER_EVENTS
            + run_events("quality", "3")
        )
        self.assertTrue(
            all(roles[r]["status"] == "SUCCEEDED" for r in ("fact_source", "design", "quality"))
        )
        self.assertTrue(all(not r["outputs_verified"] for r in roles.values()))
        self.assertEqual(roles["final"]["status"], "NOT_OBSERVED")
        complete = self.roles(
            run_events("fact_source", "1")
            + run_events("arabic", "a")
            + CHECK_EVENTS
            + run_events("design", "2")
            + RENDER_EVENTS
            + run_events("quality", "3")
            + run_events("final", "4")
        )
        self.assertEqual(complete["final"]["status"], "SUCCEEDED")
        self.assertFalse(complete["final"]["outputs_verified"])

    def test_missing_start_reused_run_and_invalid_status_rejected(self):
        events = run_events("fact_source", "1")
        for bad in (
            events[1:],
            events + events[:1],
            events + run_events("fact_source", "1"),
            run_events("fact_source", "1", status="PUBLISHED"),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.view(bad)

    def test_dependency_order_and_unbound_run_rejected(self):
        with self.assertRaises(ValueError):
            self.view(run_events("design", "1"))
        with self.assertRaises(ValueError):
            self.view(run_events("writer", "1"))

    def test_raw_errors_tokens_and_payloads_never_exported(self):
        events = run_events("fact_source", "1", status="FAILED")
        details = json.loads(events[-1]["details"])
        details.update(
            error="synthetic-secret",
            lease_token="synthetic-token",
            content={"body": "private-body"},
        )
        events[-1]["details"] = json.dumps(details)
        text = json.dumps(self.view(events))
        for secret in ("synthetic-secret", "synthetic-token", "private-body"):
            self.assertNotIn(secret, text)

    def test_future_nan_times_and_bad_attempt_rejected(self):
        for now in (9, float("nan")):
            with self.assertRaises(ValueError):
                self.view(run_events("fact_source", "1"), now=now)
        for attempt in (None, True, 0):
            with self.assertRaises(ValueError):
                self.view([], attempt=attempt)


class OwnedDatabaseProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(gc.collect)
        self.config = replace(
            Config.load(ROOT / "config/snap/weather.toml"), data_root=Path(temporary.name) / "data"
        )
        self.now = 1791000000
        self.repo = Repository(self.config, clock=lambda: self.now)
        job, _ = self.repo.enqueue(
            "contract-map", "synthetic", "did_you_know", "NORMAL", "hash", self.now + 900
        )
        self.claim = self.repo.claim(job_id=job["id"])
        self.job = job["id"]

    def event(self, event):
        with self.repo.owned_transaction(self.job, self.claim["lease_token"], attempt=1) as db:
            self.repo.event(db, self.job, event["event"], json.loads(event["details"]))

    def view(self):
        return read_snapshot(self.repo.path, self.job, now=self.now + 100, registry=REGISTRY)

    def test_real_owned_events_project_without_database_writes(self):
        for event in run_events("fact_source", "1"):
            self.event(event)
        before = self.repo.path.read_bytes()
        timeline = self.repo.timeline(self.job)
        roles = {r["id"]: r for r in self.view()["roles"]}
        self.assertEqual(roles["fact_source"]["status"], "SUCCEEDED")
        self.assertEqual(before, self.repo.path.read_bytes())
        self.assertEqual(timeline, self.repo.timeline(self.job))

    @unittest.skipUnless(
        os.name == "posix", "Owned renderer observer imports Unix HTTP server; Linux CI required"
    )
    def test_actual_observe_owned_source_is_visible(self):
        from snap_bot.did_you_know import choose_fact
        from snap_bot.render_commit import observe_owned

        content, _ = observe_owned(
            self.repo,
            self.job,
            self.claim["lease_token"],
            1,
            "fact_source",
            lambda: choose_fact(self.repo, self.job, self.claim["lease_token"], 1),
        )
        self.assertEqual(content["type"], "did_you_know")
        roles = {r["id"]: r for r in self.view()["roles"]}
        self.assertEqual(roles["fact_source"]["status"], "SUCCEEDED")
        self.assertFalse(roles["fact_source"]["outputs_verified"])

    @unittest.skipUnless(
        os.name == "posix", "Owned renderer observer imports Unix HTTP server; Linux CI required"
    )
    def test_late_observer_lease_loss_is_unknown_not_success(self):
        from snap_bot.render_commit import observe_owned

        def lose_owner():
            self.now += self.config.lease_seconds + 1
            return "not-adopted"

        with self.assertRaises(LeaseLost):
            observe_owned(
                self.repo, self.job, self.claim["lease_token"], 1, "fact_source", lose_owner
            )
        view = read_snapshot(self.repo.path, self.job, now=self.now + 200, registry=REGISTRY)
        self.assertEqual(
            next(r for r in view["roles"] if r["id"] == "fact_source")["status"], "UNKNOWN_STALE"
        )

    def test_lost_owner_cannot_create_projected_events(self):
        self.now += self.config.lease_seconds + 1
        with self.assertRaises(LeaseLost):
            self.event(run_events("fact_source", "1")[0])
        self.assertEqual(
            next(r for r in self.view()["roles"] if r["id"] == "fact_source")["status"],
            "NOT_OBSERVED",
        )

    def test_wrong_job_application_and_command_are_rejected(self):
        with self.assertRaises(ValueError):
            read_snapshot(self.repo.path, "missing", now=self.now + 100, registry=REGISTRY)
        for field, value in (("app", "status-bot-v2"), ("command", "weather")):
            with self.repo.transaction() as db:
                db.execute(f"UPDATE jobs SET {field}=? WHERE id=?", (value, self.job))
            with self.assertRaises(ValueError):
                self.view()
            with self.repo.transaction() as db:
                db.execute(
                    f"UPDATE jobs SET {field}=? WHERE id=?",
                    (self.config.app if field == "app" else "did_you_know", self.job),
                )

    def test_explicit_new_claim_selects_current_attempt(self):
        for event in run_events("fact_source", "1"):
            self.event(event)
        with self.repo.transaction() as db:
            self.repo.event(db, self.job, "job.claimed", {"attempt": 7})
        view = self.view()
        self.assertEqual(view["attempt"], 7)
        self.assertEqual(
            next(r for r in view["roles"] if r["id"] == "fact_source")["status"], "NOT_OBSERVED"
        )

    def test_read_only_connection_rejects_mutation_and_missing_db(self):
        with closing(
            sqlite3.connect(self.repo.path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM events")
        missing = self.repo.path.parent / "missing.sqlite"
        with self.assertRaises(FileNotFoundError):
            read_snapshot(missing, self.job, now=self.now + 100, registry=REGISTRY)
        self.assertFalse(missing.exists())
