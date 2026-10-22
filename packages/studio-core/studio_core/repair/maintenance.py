"""Maintenance and repair: one diagnosing engineer and two bounded workers.

The engineer reads the owned database and classifies every incident as AUTO
(a worker may fix it now), OWNER (reported, never touched) or FORBIDDEN.
Workers act only on AUTO items, inside three red lines enforced here:
  1. never resend: no job with any delivery evidence is retried, and only
     did_you_know (manual delivery behind HUMAN_FRAME) is ever re-run;
  2. never delete evidence: only INSERTs into the repair ledger and events;
  3. never bypass the owner: no human approval is written or implied.
A repair is a NEW job through the full agent chain; the failed job stays as is.

Graduated from the lab (snap_app.maintenance) with types added; the behaviour is
unchanged. The command-line entry stays in the application, which owns the runner.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from typing import Any

from studio_core.jobs.repository import GatewayLimit, Repository
from studio_core.observability.watchdog import inspect_runtime
from studio_core.repair.adoption import expire_pending_adoptions

Runner = Callable[[dict[str, Any]], dict[str, Any]]

RULE_VERSION = "maintenance-v1"
REPAIRABLE_COMMANDS = ("did_you_know",)
# Failure codes a fresh job can plausibly clear: a crash, a rejected agent
# check (a new job selects and checks again), lost ownership or an expired wait.
RETRYABLE_CODES = (
    "VERTICAL_SLICE_FAILED",
    "AGENT_CONTRACT_FAILED",
    "TELEMETRY_RECONCILIATION_REQUIRED",
    "ADOPTION_PENDING_EXPIRED",
)
MAX_REPAIRS_PER_LINEAGE = 2
MAX_REPAIRS_PER_DAY = 6
LOOKBACK_SECONDS = 86400
REPAIR_WINDOW_SECONDS = 900
LABELS = {
    "RETRY_FRESH_JOB": "إعادة المحاولة بمهمة جديدة",
    "EXPIRE_ADOPTION_WAIT": "إغلاق انتظار اعتماد منتهٍ",
    "DELIVERY_EVIDENCE_PRESENT": "ممنوع: يوجد أثر إرسال، لا إعادة إرسال",
    "DELIVERY_UNKNOWN": "ممنوع: حالة الإرسال مجهولة، لا إعادة إرسال",
    "AUTOMATIC_DELIVERY_COMMAND": "للمالك: الإعادة سترسل تلقائيًا",
    "REPAIR_BUDGET_EXHAUSTED": "للمالك: استُنفدت محاولات الإصلاح",
    "DAILY_BUDGET_EXHAUSTED": "للمالك: استُنفد حد الإصلاح اليومي",
    "UNKNOWN_FAILURE": "للمالك: سبب فشل غير معروف",
    "RUNTIME_INCIDENT": "للمالك: حادثة تشغيل تحتاج نظرًا",
}


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS maintenance_repairs(
      source_job TEXT PRIMARY KEY REFERENCES jobs(id),root_job TEXT NOT NULL,
      depth INTEGER NOT NULL,new_job TEXT NOT NULL REFERENCES jobs(id),
      code TEXT NOT NULL,repaired_at REAL NOT NULL,rule_version TEXT NOT NULL)"""
    )


def _table(db: sqlite3.Connection, name: str) -> bool:
    return (
        db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        is not None
    )


def failure_code(db: sqlite3.Connection, job: sqlite3.Row) -> str | None:
    for row in db.execute(
        "SELECT details FROM events WHERE job_id=? AND event IN "
        "('job.state','job.adoption_pending_expired') ORDER BY id DESC",
        (job["id"],),
    ):
        details = json.loads(row["details"])
        if details.get("state", "FAILED") == "FAILED" and details.get("code"):
            return str(details["code"])
    code: str | None = job["error_code"]
    return code


def delivery_evidence(db: sqlite3.Connection, job_id: str) -> bool:
    """Any trace that something may have reached Telegram for this job."""
    if db.execute("SELECT 1 FROM deliveries WHERE job_id=? LIMIT 1", (job_id,)).fetchone():
        return True
    if (
        _table(db, "telegram_outbox")
        and db.execute("SELECT 1 FROM telegram_outbox WHERE job_id=? LIMIT 1", (job_id,)).fetchone()
    ):
        return True
    return (
        db.execute(
            "SELECT 1 FROM events WHERE job_id=? AND event LIKE '%deliver%' LIMIT 1", (job_id,)
        ).fetchone()
        is not None
    )


def lineage(db: sqlite3.Connection, job_id: str) -> tuple[str, int]:
    """(root job, repairs already made on this lineage) for a failed job."""
    row = db.execute(
        "SELECT root_job,depth FROM maintenance_repairs WHERE new_job=?", (job_id,)
    ).fetchone()
    return (row["root_job"], row["depth"]) if row else (job_id, 0)


def _classify(
    db: sqlite3.Connection,
    job: sqlite3.Row,
    repaired: set[str],
    budget_used: int,
) -> dict[str, Any]:
    code = failure_code(db, job)
    item: dict[str, Any] = {"job_id": job["id"], "command": job["command"], "failure": code}
    root, depth = lineage(db, job["id"]) if repaired else (job["id"], 0)
    if code == "DELIVERY_UNKNOWN":
        item.update(decision="FORBIDDEN", reason="DELIVERY_UNKNOWN")
    elif delivery_evidence(db, job["id"]):
        item.update(decision="FORBIDDEN", reason="DELIVERY_EVIDENCE_PRESENT")
    elif job["command"] not in REPAIRABLE_COMMANDS:
        item.update(decision="OWNER", reason="AUTOMATIC_DELIVERY_COMMAND")
    elif code not in RETRYABLE_CODES:
        item.update(decision="OWNER", reason="UNKNOWN_FAILURE")
    elif depth >= MAX_REPAIRS_PER_LINEAGE:
        item.update(decision="OWNER", reason="REPAIR_BUDGET_EXHAUSTED")
    elif budget_used >= MAX_REPAIRS_PER_DAY:
        item.update(decision="OWNER", reason="DAILY_BUDGET_EXHAUSTED")
    else:
        item.update(decision="AUTO", action="RETRY_FRESH_JOB", root_job=root, depth=depth + 1)
    return item


def diagnose(repo: Repository, *, now: float | None = None) -> dict[str, Any]:
    """المهندس: a read-only classification; changes nothing."""
    now = repo.clock() if now is None else now
    items: list[dict[str, Any]] = []
    with repo.connect() as db:
        repaired = (
            {row[0] for row in db.execute("SELECT source_job FROM maintenance_repairs")}
            if _table(db, "maintenance_repairs")
            else set()
        )
        today = (
            db.execute(
                "SELECT COUNT(*) FROM maintenance_repairs WHERE repaired_at>?", (now - 86400,)
            ).fetchone()[0]
            if repaired
            else 0
        )
        planned = 0
        for job in db.execute(
            "SELECT * FROM jobs WHERE state='FAILED' AND updated_at>? ORDER BY updated_at,id",
            (now - LOOKBACK_SECONDS,),
        ).fetchall():
            if job["id"] in repaired:
                continue
            item = _classify(db, job, repaired, today + planned)
            if item["decision"] == "AUTO":
                planned += 1
            items.append(item)
        if _table(db, "render_adoption_pending"):
            for row in db.execute(
                "SELECT p.job_id FROM render_adoption_pending p JOIN jobs j ON j.id=p.job_id "
                "WHERE j.state IN ('RENDERING','RUNNING') AND j.lease_until<=?",
                (now,),
            ):
                items.append(
                    {"job_id": row["job_id"], "decision": "AUTO", "action": "EXPIRE_ADOPTION_WAIT"}
                )
    try:
        runtime = inspect_runtime(repo.path, repo.config.app, now=now)["incidents"]
    except (OSError, ValueError) as error:
        runtime = [{"kind": "WATCHDOG_UNAVAILABLE", "error": type(error).__name__}]
    for incident in runtime:
        forbidden = incident.get("delivery_uncertain") or incident.get("kind") == "DELIVERY_UNKNOWN"
        items.append(
            {
                **incident,
                "decision": "FORBIDDEN" if forbidden else "OWNER",
                "reason": "DELIVERY_UNKNOWN" if forbidden else "RUNTIME_INCIDENT",
            }
        )
    for item in items:
        item["label"] = LABELS[item.get("action") or item["reason"]]
    plan: dict[str, Any] = {
        "schema": 1,
        "app": repo.config.app,
        "rule_version": RULE_VERSION,
        "diagnosed_at": now,
        "items": items,
        "sends": False,
        "deletes": False,
        "approves": False,
    }
    plan["sha256"] = hashlib.sha256(
        json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return plan


def retry_fresh_job(repo: Repository, item: dict[str, Any], *, now: float) -> dict[str, Any]:
    """عامل الإعادة: one new job per failed job, idempotent and fenced by the red lines."""
    key = "maintenance-repair:" + item["job_id"]
    with repo.transaction() as db:
        initialize(db)
        job = db.execute("SELECT * FROM jobs WHERE id=?", (item["job_id"],)).fetchone()
        done = db.execute(
            "SELECT new_job FROM maintenance_repairs WHERE source_job=?", (item["job_id"],)
        ).fetchone()
        if done:
            return {
                "job_id": item["job_id"],
                "result": "ALREADY_REPAIRED",
                "new_job": done["new_job"],
            }
        # Re-check the red lines at the moment of action, not only at diagnosis.
        if (
            not job
            or job["state"] != "FAILED"
            or job["command"] not in REPAIRABLE_COMMANDS
            or delivery_evidence(db, job["id"])
            or failure_code(db, job) not in RETRYABLE_CODES
        ):
            return {"job_id": item["job_id"], "result": "REFUSED_RED_LINE"}
        actor = job["actor_hash"]
    try:
        new, _ = repo.enqueue(
            key,
            actor,
            job["command"],
            "NORMAL",
            hashlib.sha256(key.encode()).hexdigest(),
            now + REPAIR_WINDOW_SECONDS,
        )
    except GatewayLimit:
        return {"job_id": item["job_id"], "result": "DEFERRED_LIMIT"}
    with repo.transaction() as db:
        initialize(db)
        db.execute(
            "INSERT OR IGNORE INTO maintenance_repairs(source_job,root_job,depth,new_job,code,"
            "repaired_at,rule_version) VALUES(?,?,?,?,?,?,?)",
            (
                item["job_id"],
                item["root_job"],
                item["depth"],
                new["id"],
                item["failure"],
                now,
                RULE_VERSION,
            ),
        )
        repo.event(
            db,
            item["job_id"],
            "maintenance.repair_enqueued",
            {"new_job": new["id"], "depth": item["depth"], "code": item["failure"]},
        )
        repo.event(
            db,
            new["id"],
            "maintenance.repair_of",
            {"source_job": item["job_id"], "root_job": item["root_job"], "depth": item["depth"]},
        )
    return {"job_id": item["job_id"], "result": "ENQUEUED", "new_job": new["id"]}


def run_repair(repo: Repository, outcome: dict[str, Any], runner: Runner) -> dict[str, Any]:
    """Claim the new job and run the full agent chain once; never a delivery path."""
    claim = repo.claim(job_id=outcome["new_job"])
    if claim is None:
        return {**outcome, "run": "NOT_CLAIMED"}
    try:
        output = runner(claim)
    except Exception as error:
        # The failure is durable on the new job; the next diagnosis sees it.
        return {**outcome, "run": "FAILED", "error": type(error).__name__}
    if output.get("external_delivery") or "delivery_receipt" in output:
        raise RuntimeError("MAINTENANCE_RUNNER_DELIVERED")
    return {
        **outcome,
        "run": output["state"],
        "final_decision": (output.get("final_decision") or {}).get("delivery_decision"),
    }


def apply(
    repo: Repository,
    plan: dict[str, Any],
    *,
    now: float | None = None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """العمال: act on AUTO items of a fresh plan only; report everything else."""
    now = repo.clock() if now is None else now
    if plan.get("rule_version") != RULE_VERSION or plan.get("app") != repo.config.app:
        raise ValueError("MAINTENANCE_PLAN_INVALID")
    results = []
    if any(item.get("action") == "EXPIRE_ADOPTION_WAIT" for item in plan["items"]):
        # عامل الانتظار: the existing fenced policy; it never reclaims, renders or sends.
        for job_id in expire_pending_adoptions(repo):
            results.append({"job_id": job_id, "result": "ADOPTION_WAIT_CLOSED"})
    for item in plan["items"]:
        if item.get("action") == "RETRY_FRESH_JOB":
            outcome = retry_fresh_job(repo, item, now=now)
            if runner is not None and outcome["result"] == "ENQUEUED":
                outcome = run_repair(repo, outcome, runner)
            results.append(outcome)
    return {
        "plan_sha256": plan["sha256"],
        "results": results,
        "sends": False,
        "deletes": False,
        "approves": False,
        "owner_attention": [item for item in plan["items"] if item["decision"] != "AUTO"],
    }
