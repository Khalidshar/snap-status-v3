"""Repair inside the red lines. Ported from the lab's test_maintenance with the same guards."""

import json
import time
from pathlib import Path
from typing import Any

import pytest

from studio_core.config import Config
from studio_core.jobs import Repository
from studio_core.repair import (
    MAX_REPAIRS_PER_DAY,
    MAX_REPAIRS_PER_LINEAGE,
    RULE_VERSION,
    apply,
    claim_attempt,
    diagnose,
    expire_pending_adoptions,
    mark_pending,
)


class Lab:
    def __init__(self, root: Path) -> None:
        self.now = time.time()
        self.count = 0
        config = Config(
            "snap-bot-v2",
            root / "data",
            {"scheduler": ("owner",)},
            ("did_you_know", "weather"),
            {"CRITICAL": 180, "HIGH": 600, "NORMAL": 600, "BACKGROUND": 1200},
            30,
            60,
            100,
            10,
            60,
            60,
            86400,
            root / "proxy.sock",
            ("allowed.example",),
            2,
            4096,
            5,
        )
        self.repo = Repository(config, clock=lambda: self.now)

    def failed(self, command: str = "did_you_know", code: str = "VERTICAL_SLICE_FAILED") -> str:
        self.count += 1
        job, _ = self.repo.enqueue(
            f"job-{self.count}", f"actor-{self.count}", command, "NORMAL", "input", self.now + 900
        )
        claim = self.repo.claim(job_id=job["id"])
        assert claim
        self.repo.transition(job["id"], claim["lease_token"], "FAILED", {"code": code})
        return str(job["id"])

    def plan(self) -> dict[str, Any]:
        return diagnose(self.repo)

    def item(self, job: str) -> dict[str, Any]:
        return next(i for i in self.plan()["items"] if i.get("job_id") == job)

    def snapshot(self) -> dict[str, set[tuple[Any, ...]]]:
        with self.repo.connect() as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            # Table names come from sqlite_master, never from input.
            query = "SELECT * FROM {}"
            return {t: {tuple(row) for row in db.execute(query.format(t))} for t in tables}

    def jobs(self) -> list[dict[str, Any]]:
        with self.repo.connect() as db:
            return [
                dict(r)
                for r in db.execute("SELECT id,state,command FROM jobs ORDER BY created_at,id")
            ]


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return Lab(tmp_path)


def test_retryable_fact_failure_becomes_one_fresh_job(lab: Lab) -> None:
    job = lab.failed()
    item = lab.item(job)
    assert (item["decision"], item["action"]) == ("AUTO", "RETRY_FRESH_JOB")
    report = apply(lab.repo, lab.plan())
    new = report["results"][0]["new_job"]
    assert report["results"][0]["result"] == "ENQUEUED"
    assert lab.repo.get(new)["state"] == "QUEUED"  # type: ignore[index]
    assert lab.repo.get(job)["state"] == "FAILED"  # type: ignore[index]
    assert lab.repo.get(new)["command"] == "did_you_know"  # type: ignore[index]
    assert "maintenance.repair_enqueued" in [e["event"] for e in lab.repo.timeline(job)]
    assert json.loads(lab.repo.timeline(new)[-1]["details"])["source_job"] == job


def test_runner_runs_the_new_job_once_without_delivery(lab: Lab) -> None:
    lab.failed()
    claims: list[str] = []

    def runner(claim: dict[str, Any]) -> dict[str, Any]:
        claims.append(claim["id"])
        lab.repo.transition(
            claim["id"], claim["lease_token"], "FAILED", {"code": "VERTICAL_SLICE_FAILED"}
        )
        return {
            "state": "VALIDATING",
            "external_delivery": False,
            "final_decision": {"delivery_decision": "BLOCKED"},
        }

    result = apply(lab.repo, lab.plan(), runner=runner)["results"][0]
    assert claims == [result["new_job"]]
    assert (result["run"], result["final_decision"]) == ("VALIDATING", "BLOCKED")


def test_runner_that_delivers_is_stopped(lab: Lab) -> None:
    lab.failed()
    with pytest.raises(RuntimeError, match="MAINTENANCE_RUNNER_DELIVERED"):
        apply(
            lab.repo,
            lab.plan(),
            runner=lambda claim: {"state": "COMPLETED", "external_delivery": True},
        )


def test_runner_failure_is_recorded_not_raised(lab: Lab) -> None:
    lab.failed()

    def runner(claim: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("render crashed")

    result = apply(lab.repo, lab.plan(), runner=runner)["results"][0]
    assert (result["result"], result["run"], result["error"]) == (
        "ENQUEUED",
        "FAILED",
        "ValueError",
    )


def test_repair_is_idempotent(lab: Lab) -> None:
    job = lab.failed()
    plan = lab.plan()
    first = apply(lab.repo, plan)
    second = apply(lab.repo, plan)
    assert second["results"][0] == {
        "job_id": job,
        "result": "ALREADY_REPAIRED",
        "new_job": first["results"][0]["new_job"],
    }, "guard:repair-once"
    assert len(lab.jobs()) == 2
    assert job not in [i.get("job_id") for i in lab.plan()["items"]], (
        "guard:repaired-not-rediagnosed"
    )


@pytest.mark.parametrize("evidence", ["deliveries", "outbox", "event", "unknown"])
def test_red_line_never_resend_any_delivery_evidence(lab: Lab, evidence: str) -> None:
    job = lab.failed(code="DELIVERY_UNKNOWN" if evidence == "unknown" else "VERTICAL_SLICE_FAILED")
    with lab.repo.transaction() as db:
        if evidence == "deliveries":
            db.execute("INSERT INTO deliveries VALUES(?,?,'RESERVED')", ("d-" + job, job))
        elif evidence == "outbox":
            db.execute(
                "CREATE TABLE IF NOT EXISTS telegram_outbox("
                "job_id TEXT,video_sha TEXT,state TEXT,message_id INTEGER)"
            )
            db.execute("INSERT INTO telegram_outbox VALUES(?,'x','PENDING',NULL)", (job,))
        elif evidence == "event":
            lab.repo.event(db, job, "job.delivery_unknown", {})
    assert lab.item(job)["decision"] == "FORBIDDEN", "guard:never-resend-" + evidence
    before = len(lab.jobs())
    apply(lab.repo, lab.plan())
    assert len(lab.jobs()) == before


def test_red_line_rechecked_at_the_moment_of_action(lab: Lab) -> None:
    job = lab.failed()
    plan = lab.plan()
    assert lab.item(job)["decision"] == "AUTO"
    with lab.repo.transaction() as db:
        db.execute("INSERT INTO deliveries VALUES('late',?,'RESERVED')", (job,))
    report = apply(lab.repo, plan)
    assert report["results"] == [{"job_id": job, "result": "REFUSED_RED_LINE"}], (
        "guard:action-time-recheck"
    )
    assert len(lab.jobs()) == 1


def test_automatic_delivery_commands_go_to_the_owner(lab: Lab) -> None:
    job = lab.failed(command="weather")
    item = lab.item(job)
    assert (item["decision"], item["reason"]) == ("OWNER", "AUTOMATIC_DELIVERY_COMMAND"), (
        "guard:weather-never-auto"
    )
    apply(lab.repo, lab.plan())
    assert len(lab.jobs()) == 1


def test_unknown_failures_go_to_the_owner(lab: Lab) -> None:
    job = lab.failed(code="SOMETHING_NEW")
    assert lab.item(job)["reason"] == "UNKNOWN_FAILURE", "guard:unknown-code"


def test_red_line_never_deletes_or_changes_existing_evidence(lab: Lab) -> None:
    for _ in range(3):
        lab.failed()
    lab.failed(command="weather")
    before = lab.snapshot()
    apply(lab.repo, lab.plan())
    after = lab.snapshot()
    for table, rows in before.items():
        assert rows <= after[table], "guard:never-delete " + table


def test_red_line_never_writes_owner_approval(lab: Lab) -> None:
    lab.failed()
    report = apply(lab.repo, lab.plan())
    assert not report["approves"] and not report["sends"] and not report["deletes"]
    with lab.repo.connect() as db:
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='human_frame_reviews'").fetchone()
            is None
        )
        assert db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0


def test_lineage_budget_stops_endless_retries(lab: Lab) -> None:
    job = lab.failed()
    for depth in range(1, MAX_REPAIRS_PER_LINEAGE + 1):
        assert lab.item(job)["depth"] == depth
        new = apply(lab.repo, lab.plan())["results"][0]["new_job"]
        claim = lab.repo.claim(job_id=new)
        assert claim
        lab.repo.transition(new, claim["lease_token"], "FAILED", {"code": "AGENT_CONTRACT_FAILED"})
        job = new
    assert lab.item(job)["reason"] == "REPAIR_BUDGET_EXHAUSTED", "guard:lineage-budget"


def test_daily_budget_caps_repairs(lab: Lab) -> None:
    jobs = [lab.failed() for _ in range(MAX_REPAIRS_PER_DAY + 1)]
    decisions = [lab.item(job)["decision"] for job in jobs]
    assert decisions.count("AUTO") == MAX_REPAIRS_PER_DAY, "guard:daily-budget"
    held = next(job for job, decision in zip(jobs, decisions, strict=True) if decision == "OWNER")
    assert lab.item(held)["reason"] == "DAILY_BUDGET_EXHAUSTED"
    apply(lab.repo, lab.plan())
    assert lab.item(held)["reason"] == "DAILY_BUDGET_EXHAUSTED"
    assert len(lab.jobs()) == 2 * MAX_REPAIRS_PER_DAY + 1


def test_old_failures_are_outside_the_lookback(lab: Lab) -> None:
    job = lab.failed()
    lab.now += 86401
    assert job not in [i.get("job_id") for i in lab.plan()["items"]]


def test_expired_adoption_wait_is_closed_without_reclaim(lab: Lab) -> None:
    job, _ = lab.repo.enqueue(
        "wait", "actor-wait", "did_you_know", "NORMAL", "input", lab.now + 900
    )
    claim = lab.repo.claim(job_id=job["id"])
    assert claim
    for state in ("RESEARCHING", "PROCESSING", "DOWNLOADING", "DESIGNING", "RENDERING"):
        lab.repo.transition(job["id"], claim["lease_token"], state)
    mark_pending(lab.repo, job["id"], claim["lease_token"], 1)
    assert "EXPIRE_ADOPTION_WAIT" not in [i.get("action") for i in lab.plan()["items"]]
    lab.now += lab.repo.config.lease_seconds + 1
    assert "EXPIRE_ADOPTION_WAIT" in [i.get("action") for i in lab.plan()["items"]]
    report = apply(lab.repo, lab.plan())
    assert {"job_id": job["id"], "result": "ADOPTION_WAIT_CLOSED"} in report["results"], (
        "guard:close-expired-wait"
    )
    assert lab.repo.get(job["id"])["state"] == "FAILED"  # type: ignore[index]


def test_adoption_wait_with_delivery_evidence_is_left_to_reconciliation(lab: Lab) -> None:
    job, _ = lab.repo.enqueue(
        "wait", "actor-wait", "did_you_know", "NORMAL", "input", lab.now + 900
    )
    claim = lab.repo.claim(job_id=job["id"])
    assert claim
    for state in ("RESEARCHING", "PROCESSING", "DOWNLOADING", "DESIGNING", "RENDERING"):
        lab.repo.transition(job["id"], claim["lease_token"], state)
    mark_pending(lab.repo, job["id"], claim["lease_token"], 1)
    with lab.repo.transaction() as db:
        db.execute("INSERT INTO deliveries VALUES('d',?,'RESERVED')", (job["id"],))
    lab.now += lab.repo.config.lease_seconds + 1
    assert expire_pending_adoptions(lab.repo) == [], "guard:adoption-delivery-evidence"
    assert lab.repo.get(job["id"])["state"] == "RENDERING"  # type: ignore[index]


def test_pending_marker_refuses_a_stale_owner(lab: Lab) -> None:
    from studio_core.jobs import LeaseLost

    job, _ = lab.repo.enqueue(
        "wait", "actor-wait", "did_you_know", "NORMAL", "input", lab.now + 900
    )
    claim = lab.repo.claim(job_id=job["id"])
    assert claim
    for state in ("RESEARCHING", "PROCESSING", "DOWNLOADING", "DESIGNING", "RENDERING"):
        lab.repo.transition(job["id"], claim["lease_token"], state)
    with pytest.raises(LeaseLost):
        mark_pending(lab.repo, job["id"], "not-the-owner", 1)
    with pytest.raises(LeaseLost):
        mark_pending(lab.repo, job["id"], claim["lease_token"], 2)
    for bad in (0, 101, True):
        with pytest.raises(ValueError, match="expiry batch"):
            expire_pending_adoptions(lab.repo, limit=bad)


def test_claim_attempt_prefers_the_explicit_generation() -> None:
    assert claim_attempt({"attempt": 3}, 1) == 3
    assert claim_attempt({}, 2) == 2
    for bad in ({"attempt": 0}, {"attempt": "1"}, {"attempt": True}):
        with pytest.raises(ValueError):
            claim_attempt(bad, 1)


def test_runtime_incidents_are_reported_and_delivery_uncertainty_forbidden(lab: Lab) -> None:
    job, _ = lab.repo.enqueue("stuck", "actor-stuck", "weather", "NORMAL", "input", lab.now + 900)
    claim = lab.repo.claim(job_id=job["id"])
    assert claim
    for state in (
        "RESEARCHING",
        "PROCESSING",
        "DOWNLOADING",
        "DESIGNING",
        "RENDERING",
        "VALIDATING",
        "DELIVERING",
    ):
        lab.repo.transition(job["id"], claim["lease_token"], state)
    lab.now += lab.repo.config.lease_seconds + 1
    incident = next(
        i for i in lab.plan()["items"] if i.get("kind") == "JOB_DEADLINE_OR_LEASE_EXPIRED"
    )
    assert incident["decision"] == "FORBIDDEN", "guard:delivery-uncertain"
    before = lab.snapshot()
    apply(lab.repo, lab.plan())
    assert before == lab.snapshot()


def test_diagnosis_changes_nothing(lab: Lab) -> None:
    lab.failed()
    before = lab.snapshot()
    plan = lab.plan()
    assert before == lab.snapshot()
    assert plan["rule_version"] == RULE_VERSION


@pytest.mark.parametrize("change", [{"rule_version": "old"}, {"app": "status-bot-v2"}])
def test_foreign_plan_is_refused(lab: Lab, change: dict[str, str]) -> None:
    lab.failed()
    with pytest.raises(ValueError, match="MAINTENANCE_PLAN_INVALID"):
        apply(lab.repo, {**lab.plan(), **change})
    assert len(lab.jobs()) == 1
