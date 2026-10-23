"""Four owned fact contracts. No execution retries, recovery or network here."""

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from studio_core.jobs import LeaseLost, Repository

from .agent_contract_map import (
    BOUND,
    CONTENT_CHECKS,
    DEPENDENCIES,
    PASS_THROUGH,
    RENDER_CHECKS,
    contract_plan,
    validate_payload,
)


class ContractViolation(ValueError):
    pass


class ContractReconciliationRequired(RuntimeError):
    pass


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def initialize(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS owned_agent_contracts(
      job_id TEXT NOT NULL,attempt INTEGER NOT NULL,role TEXT NOT NULL,
      run TEXT NOT NULL,plan_sha TEXT NOT NULL,input_sha TEXT NOT NULL,
      output_sha TEXT,status TEXT NOT NULL,error_code TEXT,reconciled_by INTEGER,
      PRIMARY KEY(job_id,attempt,role))""")
    if "output_json" not in {
        row["name"] for row in db.execute("PRAGMA table_info(owned_agent_contracts)")
    }:
        db.execute("ALTER TABLE owned_agent_contracts ADD COLUMN output_json TEXT")


def scope(repo: Repository, db: sqlite3.Connection, job: str) -> None:
    row = db.execute("SELECT app,command FROM jobs WHERE id=?", (job,)).fetchone()
    if (
        repo.config.app != "snap-bot-v2"
        or not row
        or row["app"] != "snap-bot-v2"
        or row["command"] != "did_you_know"
    ):
        raise ContractViolation("CONTRACT_SCOPE_INVALID")


def validate(
    role: str, direction: str, payload: Any, job: str, attempt: int, original: Any = None
) -> Any:
    try:
        validate_payload(role, direction, payload)
        if (
            (role == "design" and direction == "output")
            or role == "quality"
            or role in RENDER_CHECKS
        ):
            if payload.get("job_id") != job or payload.get("attempt") != attempt:
                raise ValueError("Foreign output")
        if (
            direction == "output"
            and role == "design"
            and payload["content_hash"] != original["content_hash"]
        ):
            raise ValueError("Foreign content")
        if direction == "output" and role in PASS_THROUGH and digest(payload) != digest(original):
            raise ValueError("Check agent rewrote content")
        if (
            direction == "output"
            and role == "quality"
            and (
                payload["sha256"] != original["video_sha256"]
                or payload["path"] != original["artifact"]
            )
        ):
            raise ValueError("Foreign adoption")
    except (ValueError, TypeError, KeyError, AttributeError) as cause:
        raise ContractViolation("CONTRACT_" + direction.upper() + "_INVALID") from cause


def output_payload(role: str, result: Any) -> Any:
    if role == "fact_source":
        if not isinstance(result, tuple) or len(result) != 2 or not isinstance(result[1], list):
            raise ContractViolation("CONTRACT_OUTPUT_INVALID")
        return result[0]
    return result


def telemetry(
    repo: Repository,
    job: str,
    token: str,
    attempt: int,
    role: str,
    run: str,
    status: str,
    started: float,
    code: str | None = None,
) -> None:
    with repo.owned_transaction(job, token, attempt=attempt) as db:
        details = {
            "agent": role,
            "attempt": attempt,
            "run": run,
            "status": status,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if code:
            details["error_code"] = code
        repo.event(db, job, "agent.run", details)


def run_stage(
    repo: Repository,
    job: str,
    token: str,
    attempt: int,
    role: str,
    payload: Any,
    callback: Callable[[], Any],
    *,
    retry_adoption: bool = False,
    retryable_errors: tuple[type[BaseException], ...] = (),
) -> Any:
    if role not in BOUND:
        raise ContractViolation("CONTRACT_ROLE_INVALID")
    run = uuid.uuid4().hex
    started = time.monotonic()
    input_sha = digest(payload)
    problem = None
    with repo.owned_transaction(job, token, attempt=attempt) as db:
        scope(repo, db, job)
        initialize(db)
        previous = db.execute(
            "SELECT * FROM owned_agent_contracts WHERE job_id=? AND attempt=? AND role=?",
            (job, attempt, role),
        ).fetchone()
        if previous and not (
            retry_adoption
            and role == "quality"
            and previous["status"] == "WAITING_ADOPTION"
            and previous["input_sha"] == input_sha
        ):
            raise ContractReconciliationRequired("CONTRACT_ALREADY_STARTED; no callback replay")
        try:
            validate(role, "input", payload, job, attempt)
            for dep in DEPENDENCIES[role]:
                prior = db.execute(
                    "SELECT status,output_sha FROM owned_agent_contracts WHERE job_id=? AND attempt=? AND role=?",
                    (job, attempt, dep),
                ).fetchone()
                if not prior or prior["status"] != "SUCCEEDED" or prior["output_sha"] != input_sha:
                    raise ContractViolation("CONTRACT_DEPENDENCY_INVALID")
        except ContractViolation as error:
            problem = error
        db.execute(
            "INSERT OR REPLACE INTO owned_agent_contracts(job_id,attempt,role,run,plan_sha,input_sha,status,error_code) VALUES(?,?,?,?,?,?,?,?)",
            (
                job,
                attempt,
                role,
                run,
                contract_plan()["sha256"],
                input_sha,
                "FAILED" if problem else "READY",
                str(problem) if problem else None,
            ),
        )
    if problem:
        raise problem
    try:
        telemetry(repo, job, token, attempt, role, run, "RUNNING", started)
    except LeaseLost:
        raise
    except Exception as error:
        raise ContractReconciliationRequired("CONTRACT_TELEMETRY_START_PENDING") from error
    with repo.owned_transaction(job, token, attempt=attempt) as db:
        db.execute(
            "UPDATE owned_agent_contracts SET status='RUNNING' WHERE job_id=? AND attempt=? AND role=? AND run=?",
            (job, attempt, role, run),
        )
    try:
        result = callback()
        with repo.owned_transaction(job, token, attempt=attempt) as db:
            scope(repo, db, job)
            try:
                value = output_payload(role, result)
                validate(role, "output", value, job, attempt, payload)
            except ContractViolation as error:
                problem = error
                db.execute(
                    "UPDATE owned_agent_contracts SET status='FAILED',error_code=? WHERE job_id=? AND attempt=? AND role=?",
                    (str(error), job, attempt, role),
                )
            else:
                db.execute(
                    "UPDATE owned_agent_contracts SET status='SUCCEEDED',output_sha=? WHERE job_id=? AND attempt=? AND role=?",
                    (digest(value), job, attempt, role),
                )
                if role == "arabic":
                    from .arabic_agent import inspect_content

                    saved = {
                        "fact_id": value["fact_id"],
                        "body": value["body"],
                        "content_hash": value["content_hash"],
                        "content_sha": digest(value),
                        "report": inspect_content(value),
                    }
                    db.execute(
                        "UPDATE owned_agent_contracts SET output_json=? WHERE job_id=? AND attempt=? AND role=?",
                        (json.dumps(saved, ensure_ascii=False, sort_keys=True), job, attempt, role),
                    )
        if problem:
            raise problem
    except LeaseLost:
        # A committed adoption may exist. The new owner must inspect it; no replay.
        raise
    except Exception as error:
        waiting = role == "quality" and isinstance(error, retryable_errors)
        with repo.owned_transaction(job, token, attempt=attempt) as db:
            db.execute(
                "UPDATE owned_agent_contracts SET status=?,error_code=? WHERE job_id=? AND attempt=? AND role=?",
                (
                    "WAITING_ADOPTION" if waiting else "FAILED",
                    "ADOPTION_PENDING"
                    if waiting
                    else "CONTRACT_OUTPUT_INVALID"
                    if problem
                    else "CONTRACT_ARABIC_REJECTED"
                    if role == "arabic" and isinstance(error, ValueError)
                    else "CONTRACT_CHECK_REJECTED"
                    if role in PASS_THROUGH and isinstance(error, ValueError)
                    else "CONTRACT_STAGE_FAILED",
                    job,
                    attempt,
                    role,
                ),
            )
        try:
            telemetry(repo, job, token, attempt, role, run, "FAILED", started, "STAGE_FAILED")
        except Exception:
            pass  # The durable contract failure, not logging, blocks delivery.
        if role == "arabic" and isinstance(error, ValueError) and not problem:
            raise ContractViolation("CONTRACT_ARABIC_REJECTED") from error
        if role in PASS_THROUGH and isinstance(error, ValueError) and not problem:
            raise ContractViolation("CONTRACT_CHECK_REJECTED") from error
        raise
    try:
        telemetry(repo, job, token, attempt, role, run, "SUCCEEDED", started)
    except LeaseLost:
        raise
    except Exception as error:
        # SUCCEEDED contract outcome committed before optional observer recording.
        raise ContractReconciliationRequired("CONTRACT_TELEMETRY_SUCCESS_PENDING") from error
    return result


def contract_status(repo: Repository, job: str, attempt: int, role: str) -> Any:
    with repo.connect() as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='owned_agent_contracts'"
        ).fetchone():
            return None
        row = db.execute(
            "SELECT status FROM owned_agent_contracts WHERE job_id=? AND attempt=? AND role=?",
            (job, attempt, role),
        ).fetchone()
        return row["status"] if row else None


def arabic_evidence(row: Any, content_sha: str, content_hash: str | None = None) -> Any:
    from .arabic_agent import inspect_content
    from .did_you_know import load_facts, make_content

    try:
        saved = json.loads(row["output_json"])
        if (
            row["plan_sha"] != contract_plan()["sha256"]
            or row["status"] != "SUCCEEDED"
            or row["input_sha"] != content_sha
            or row["output_sha"] != content_sha
            or saved["content_sha"] != content_sha
        ):
            raise ValueError("Arabic lineage changed")
        fact = next((f for f in load_facts() if f["id"] == saved["fact_id"]), None)
        if (
            fact is None
            or fact["text"] != saved["body"]
            or make_content(fact, 0)["content_hash"] != saved["content_hash"]
        ):
            raise ValueError("Foreign Arabic fact")
        if content_hash is not None and saved["content_hash"] != content_hash:
            raise ValueError("Foreign Arabic content")
        if saved["report"] != inspect_content(
            {"type": "did_you_know", "sacred": False, "body": saved["body"]}
        ):
            raise ValueError("Arabic proof changed")
    except (TypeError, ValueError, KeyError, IndexError) as cause:
        raise ContractViolation("CONTRACT_ARABIC_EVIDENCE_INVALID") from cause


def check_chain(rows: Any) -> Any:
    """Every content check agent succeeded on the exact source content and passed it through."""
    content_sha = rows["fact_source"]["output_sha"]
    for role in CONTENT_CHECKS:
        row = rows.get(role)
        if (
            not row
            or row["status"] != "SUCCEEDED"
            or row["input_sha"] != content_sha
            or row["output_sha"] != content_sha
        ):
            raise ContractViolation("CONTRACT_CHECK_CHAIN_INVALID")


def render_check_chain(rows: Any, result: Any) -> Any:
    """Every render check agent succeeded on the exact render result and passed it through."""
    result_sha = digest(result)
    for role in RENDER_CHECKS:
        row = rows.get(role)
        if (
            not row
            or row["status"] != "SUCCEEDED"
            or row["input_sha"] != result_sha
            or row["output_sha"] != result_sha
        ):
            raise ContractViolation("CONTRACT_RENDER_CHECK_CHAIN_INVALID")


def adoption_receipt(db: sqlite3.Connection, job: str, attempt: int, result: Any) -> Any:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='stage_results'").fetchone():
        raise ContractViolation("CONTRACT_ADOPTION_UNCONFIRMED")
    rows = {
        row["stage"]: dict(row)
        for row in db.execute("SELECT * FROM stage_results WHERE job_id=?", (job,))
    }
    if "design" not in rows or "quality" not in rows:
        raise ContractViolation("CONTRACT_ADOPTION_UNCONFIRMED")
    receipt = rows["design"]
    validate("quality", "output", receipt, job, attempt, result)
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2).encode()
    expected_path = str(Path(result["artifact"]).parent / "render-result.json").replace("\\", "/")
    quality = rows["quality"]
    if (
        quality["attempt"] != attempt
        or quality["path"] != expected_path
        or quality["sha256"] != hashlib.sha256(encoded).hexdigest()
    ):
        raise ContractViolation("CONTRACT_QUALITY_RECEIPT_INVALID")
    return receipt


def reconcile_stage(
    repo: Repository,
    job: str,
    token: str,
    current_attempt: int,
    role: str,
    source_attempt: int,
    *,
    settings: Any = None,
    result: Any = None,
) -> dict[str, Any]:
    """Explicit ledger reconciliation only. Never callback, render, adopt or send.

    RUNNING quality can be resolved from an already committed stage receipt and
    immutable artifacts. A foreign epoch is recorded as evidence, not new work.
    """
    if (
        role not in BOUND
        or type(source_attempt) is not int
        or not 1 <= source_attempt <= current_attempt
    ):
        raise ContractViolation("CONTRACT_RECONCILIATION_SCOPE_INVALID")
    repo.assert_owner(job, token, attempt=current_attempt)
    content = None
    if role == "quality":
        if settings is None or result is None:
            raise ContractViolation("CONTRACT_RECONCILIATION_EVIDENCE_MISSING")
        validate(role, "input", result, job, source_attempt)
        from .n1_artifacts import verified_bytes

        root = Path(settings["spool_root"])
        if not root.is_absolute() or root.name != "renders":
            raise ContractViolation("CONTRACT_SPOOL_INVALID")
        video = root.parent / result["artifact"]
        for path in (root, *video.parents, video):
            if path.is_symlink():
                raise ContractViolation("CONTRACT_SPOOL_INVALID")
        verified_bytes(video, result["video_sha256"])
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2).encode()
        verified_bytes(video.parent / "render-result.json", hashlib.sha256(encoded).hexdigest())
        input_root = Path(settings["input_root"])
        if not input_root.is_absolute() or input_root.name != "renders":
            raise ContractViolation("CONTRACT_INPUT_ROOT_INVALID")
        input_path = input_root / job / "attempts" / str(source_attempt) / "input/content.json"
        for path in (input_root, *input_path.parents, input_path):
            if path.is_symlink():
                raise ContractViolation("CONTRACT_INPUT_ROOT_INVALID")
        content = json.loads(input_path.read_bytes())
    with repo.owned_transaction(job, token, attempt=current_attempt) as db:
        scope(repo, db, job)
        initialize(db)
        row = db.execute(
            "SELECT * FROM owned_agent_contracts WHERE job_id=? AND attempt=? AND role=?",
            (job, source_attempt, role),
        ).fetchone()
        if not row or row["plan_sha"] != contract_plan()["sha256"]:
            raise ContractViolation("CONTRACT_RECONCILIATION_EVIDENCE_MISSING")
        if role == "arabic":
            source = db.execute(
                "SELECT * FROM owned_agent_contracts WHERE job_id=? AND attempt=? AND role='fact_source'",
                (job, source_attempt),
            ).fetchone()
            if (
                not source
                or source["status"] != "SUCCEEDED"
                or source["plan_sha"] != row["plan_sha"]
                or source["output_sha"] != row["input_sha"]
            ):
                raise ContractViolation("CONTRACT_DEPENDENCY_INVALID")
            arabic_evidence(row, source["output_sha"])
        if role == "quality":
            if row["status"] not in ("RUNNING", "SUCCEEDED"):
                raise ContractViolation("CONTRACT_NOT_RECONCILABLE")
            validate("design", "input", content, job, source_attempt)
            source = db.execute(
                "SELECT status,output_sha FROM owned_agent_contracts WHERE job_id=? AND attempt=? AND role='fact_source'",
                (job, source_attempt),
            ).fetchone()
            language = db.execute(
                "SELECT * FROM owned_agent_contracts WHERE job_id=? AND attempt=? AND role='arabic'",
                (job, source_attempt),
            ).fetchone()
            if not language:
                raise ContractViolation("CONTRACT_ARABIC_EVIDENCE_INVALID")
            arabic_evidence(language, digest(content), result["content_hash"])
            checks = {
                r["role"]: dict(r)
                for r in db.execute(
                    "SELECT * FROM owned_agent_contracts WHERE job_id=? AND attempt=?",
                    (job, source_attempt),
                )
            }
            if not source:
                raise ContractViolation("CONTRACT_DEPENDENCY_INVALID")
            check_chain({**checks, "fact_source": dict(source)})
            render_check_chain(checks, result)
            design = db.execute(
                "SELECT status,input_sha,output_sha FROM owned_agent_contracts WHERE job_id=? AND attempt=? AND role='design'",
                (job, source_attempt),
            ).fetchone()
            if (
                not source
                or not design
                or source["status"] != "SUCCEEDED"
                or design["status"] != "SUCCEEDED"
                or source["output_sha"] != digest(content)
                or design["input_sha"] != digest(content)
                or design["output_sha"] != digest(result)
                or row["input_sha"] != digest(result)
            ):
                raise ContractViolation("CONTRACT_RECONCILIATION_BINDING_INVALID")
            receipt = adoption_receipt(db, job, source_attempt, result)
            if row["status"] == "SUCCEEDED" and row["output_sha"] != digest(receipt):
                raise ContractViolation("CONTRACT_RECEIPT_CHANGED")
            db.execute(
                "UPDATE owned_agent_contracts SET status='SUCCEEDED',output_sha=?,reconciled_by=? WHERE job_id=? AND attempt=? AND role=?",
                (digest(receipt), current_attempt, job, source_attempt, role),
            )
        elif row["status"] != "SUCCEEDED":
            raise ContractViolation("CONTRACT_NOT_RECONCILABLE")
        if row["reconciled_by"] != current_attempt:
            if current_attempt == source_attempt:
                runs = [
                    json.loads(event["details"])
                    for event in db.execute(
                        "SELECT details FROM events WHERE job_id=? AND event='agent.run' ORDER BY id",
                        (job,),
                    )
                ]
                matching = [
                    item
                    for item in runs
                    if item.get("run") == row["run"]
                    and item.get("agent") == role
                    and item.get("attempt") == source_attempt
                ]
                if not matching or matching[-1]["status"] not in ("RUNNING", "SUCCEEDED"):
                    raise ContractViolation("CONTRACT_OBSERVER_SEQUENCE_INVALID")
                if matching[-1]["status"] == "RUNNING":
                    repo.event(
                        db,
                        job,
                        "agent.run",
                        {
                            "agent": role,
                            "attempt": source_attempt,
                            "run": row["run"],
                            "status": "SUCCEEDED",
                            "duration_ms": 0,
                            "reconciled": True,
                        },
                    )
            repo.event(
                db,
                job,
                "agent.contract_reconciled",
                {
                    "agent": role,
                    "attempt": current_attempt,
                    "evidence_attempt": source_attempt,
                    "status": "SUCCEEDED",
                },
            )
            db.execute(
                "UPDATE owned_agent_contracts SET reconciled_by=? WHERE job_id=? AND attempt=? AND role=?",
                (current_attempt, job, source_attempt, role),
            )
    return {
        "role": role,
        "evidence_attempt": source_attempt,
        "owner_attempt": current_attempt,
        "status": "RECONCILED",
        "execution_retried": False,
        "delivery_attempted": False,
    }


def legacy_preview_at_creation(db: sqlite3.Connection, job: str) -> bool:
    """Only an explicit policy on the original creation event is legacy evidence.

    No runtime path writes this policy or backfills existing jobs. A missing
    ledger, old timestamp, file or later event is never a legacy designation.
    """
    row = db.execute("SELECT app,command,created_at FROM jobs WHERE id=?", (job,)).fetchone()
    event = db.execute(
        "SELECT event,at,details FROM events WHERE job_id=? ORDER BY id LIMIT 1", (job,)
    ).fetchone()
    if (
        not row
        or row["app"] != "snap-bot-v2"
        or row["command"] != "did_you_know"
        or not event
        or event["event"] != "job.queued"
        or event["at"] < row["created_at"]
    ):
        return False
    # enqueue timestamps the row and event with two clock reads in one creation
    # transaction. Require event ordering, not exact floating-point equality.
    claim = db.execute(
        "SELECT at FROM events WHERE job_id=? AND event='job.claimed' ORDER BY id LIMIT 1", (job,)
    ).fetchone()
    if claim and event["at"] > claim["at"]:
        return False
    try:
        details = json.loads(event["details"])
    except (ValueError, TypeError):
        return False
    return (
        isinstance(details, dict)
        and details.get("command") == "did_you_know"
        and details.get("contract_policy") == "legacy-preview-before-adapter-v1"
    )


def require_delivery_contract(db: sqlite3.Connection, job: str, result: Any) -> None:
    """Fail closed without contracts unless creation explicitly designated legacy.
    An ACK is authoritative even if subsequent telemetry or contracts are broken.
    """
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='owned_agent_contracts'").fetchone()
    epoch = (
        db.execute(
            "SELECT MAX(attempt) FROM owned_agent_contracts WHERE job_id=?", (job,)
        ).fetchone()[0]
        if exists
        else None
    )
    if epoch is None:
        if legacy_preview_at_creation(db, job):
            return
        raise ContractViolation("CONTRACT_DELIVERY_BLOCKED")
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_outbox'").fetchone():
        if db.execute(
            "SELECT 1 FROM telegram_outbox o JOIN deliveries d ON d.job_id=? AND d.state='PUBLISHED' AND d.id IN (o.video_sha,o.video_sha||':'||?) WHERE o.video_sha=? AND o.state='PUBLISHED' AND typeof(o.message_id)='integer'",
            (job, job, result["video_sha256"]),
        ).fetchone():
            return
    quality_chain(db, job, epoch, result)
    from .final_agent import require_final

    require_final(db, job, epoch, result)


def quality_chain(db: sqlite3.Connection, job: str, epoch: int, result: Any) -> dict[str, Any]:
    rows = {
        r["role"]: dict(r)
        for r in db.execute(
            "SELECT * FROM owned_agent_contracts WHERE job_id=? AND attempt=? AND role!='final'",
            (job, epoch),
        )
    }
    if any(role not in rows or rows[role]["status"] != "SUCCEEDED" for role in BOUND):
        raise ContractViolation("CONTRACT_DELIVERY_BLOCKED")
    if any(row["plan_sha"] != contract_plan()["sha256"] for row in rows.values()):
        raise ContractViolation("CONTRACT_PLAN_CHANGED")
    arabic_evidence(rows["arabic"], rows["fact_source"]["output_sha"], result["content_hash"])
    check_chain(rows)
    render_check_chain(rows, result)
    validate("quality", "input", result, job, epoch)
    receipt = adoption_receipt(db, job, epoch, result)
    if (
        rows["fact_source"]["output_sha"] != rows["design"]["input_sha"]
        or rows["design"]["output_sha"] != digest(result)
        or rows["quality"]["input_sha"] != digest(result)
        or rows["quality"]["output_sha"] != digest(receipt)
    ):
        raise ContractViolation("CONTRACT_DELIVERY_BINDING_INVALID")
    return {
        "render": result,
        "receipt": receipt,
        "contracts": {
            role: {key: rows[role][key] for key in ("plan_sha", "input_sha", "output_sha")}
            for role in BOUND
        },
    }
