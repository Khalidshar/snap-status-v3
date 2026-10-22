"""Owned final decision and explicit local HUMAN_FRAME command. Never sends."""

import argparse
import json
import math
import re
import time
import uuid
from pathlib import Path

from studio_core.config import Config
from studio_core.jobs import Repository

from .agent_contract_map import contract_plan
from .owned_contract_adapter import (
    ContractReconciliationRequired,
    ContractViolation,
    digest,
    initialize,
    quality_chain,
    scope,
    telemetry,
)


def initialize_reviews(db):
    db.execute("""CREATE TABLE IF NOT EXISTS human_frame_reviews(
      id TEXT PRIMARY KEY,job_id TEXT NOT NULL,attempt INTEGER NOT NULL,
      reviewer TEXT NOT NULL,reviewed_at REAL NOT NULL,video_sha TEXT NOT NULL,
      content_hash TEXT NOT NULL,quality_sha TEXT NOT NULL,mode TEXT NOT NULL)""")


def record_human_frame(repo, job, token, attempt, result, *, reviewer):
    """Only explicit operator command calls this; no preview/runtime caller."""
    if reviewer not in repo.config.owners.get("telegram", ()):
        raise ContractViolation("FINAL_REVIEWER_UNAUTHORIZED")
    with repo.owned_transaction(job, token, attempt=attempt) as db:
        scope(repo, db, job)
        initialize(db)
        initialize_reviews(db)
        state = db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone()["state"]
        if state != "VALIDATING":
            raise ContractViolation("FINAL_REVIEW_STATE_INVALID")
        evidence = quality_chain(db, job, attempt, result)
        now = repo.clock()
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ContractViolation("FINAL_REVIEW_TIME_INVALID")
        review = {
            "id": uuid.uuid4().hex,
            "job_id": job,
            "attempt": attempt,
            "reviewer": reviewer,
            "reviewed_at": now,
            "video_sha": result["video_sha256"],
            "content_hash": result["content_hash"],
            "quality_sha": digest(evidence),
            "mode": "HUMAN_FRAME",
        }
        db.execute(
            "INSERT INTO human_frame_reviews(id,job_id,attempt,reviewer,reviewed_at,video_sha,content_hash,quality_sha,mode) VALUES(?,?,?,?,?,?,?,?,?)",
            tuple(
                review[key]
                for key in (
                    "id",
                    "job_id",
                    "attempt",
                    "reviewer",
                    "reviewed_at",
                    "video_sha",
                    "content_hash",
                    "quality_sha",
                    "mode",
                )
            ),
        )
        repo.event(db, job, "human.frame.approved", review)
    return review


def final_input(db, job, attempt, result, now):
    evidence = quality_chain(db, job, attempt, result)
    review = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='human_frame_reviews'").fetchone():
        row = db.execute(
            "SELECT * FROM human_frame_reviews WHERE job_id=? ORDER BY reviewed_at DESC,rowid DESC LIMIT 1",
            (job,),
        ).fetchone()
        if row:
            review = dict(row)
    return {
        "job_id": job,
        "attempt": attempt,
        "evidence": evidence,
        "human_approval": review,
        "evaluated_at": now,
    }


def decide(payload):
    review = payload["human_approval"]
    render = payload["evidence"]["render"]
    reason = "HUMAN_FRAME_MISSING"
    if review:
        valid = (
            review["mode"] == "HUMAN_FRAME"
            and review["job_id"] == payload["job_id"]
            and review["attempt"] == payload["attempt"]
            and review["video_sha"] == render["video_sha256"]
            and review["content_hash"] == render["content_hash"]
            and review["quality_sha"] == digest(payload["evidence"])
            and bool(review["reviewer"])
            and isinstance(review["reviewed_at"], (int, float))
            and math.isfinite(review["reviewed_at"])
            and 0 <= payload["evaluated_at"] - review["reviewed_at"] <= 86400
        )
        reason = "HUMAN_FRAME_MATCHED" if valid else "HUMAN_FRAME_MISMATCH_OR_EXPIRED"
    return {
        "job_id": payload["job_id"],
        "attempt": payload["attempt"],
        "video_sha": render["video_sha256"],
        "content_hash": render["content_hash"],
        "quality_sha": digest(payload["evidence"]),
        "review_id": review["id"] if review else None,
        "delivery_decision": "APPROVED" if reason == "HUMAN_FRAME_MATCHED" else "BLOCKED",
        "reason": reason,
    }


def validate_final_payload(direction, payload):
    if not isinstance(payload, dict):
        raise ValueError("Final payload required")
    if (
        not isinstance(payload.get("job_id"), str)
        or not re.fullmatch("[0-9a-f]{32}", payload["job_id"])
        or type(payload.get("attempt")) is not int
        or payload["attempt"] < 1
    ):
        raise ValueError("Invalid final identity")
    if direction == "input":
        if set(payload) != {"job_id", "attempt", "evidence", "human_approval", "evaluated_at"}:
            raise ValueError("Incomplete final input")
        if not isinstance(payload["evaluated_at"], (int, float)) or not math.isfinite(
            payload["evaluated_at"]
        ):
            raise ValueError("Invalid final time")
        evidence = payload["evidence"]
        if not isinstance(evidence, dict) or set(evidence) != {"render", "receipt", "contracts"}:
            raise ValueError("Invalid quality evidence")
        from .agent_contract_map import BOUND, validate_payload

        validate_payload("quality", "input", evidence["render"])
        validate_payload("quality", "output", evidence["receipt"])
        if set(evidence["contracts"]) != set(BOUND):
            raise ValueError("Incomplete quality chain")
        review = payload["human_approval"]
        if review is not None and (
            not isinstance(review, dict)
            or set(review)
            != {
                "id",
                "job_id",
                "attempt",
                "reviewer",
                "reviewed_at",
                "video_sha",
                "content_hash",
                "quality_sha",
                "mode",
            }
        ):
            raise ValueError("Incomplete human review")
    else:
        if set(payload) != {
            "job_id",
            "attempt",
            "video_sha",
            "content_hash",
            "quality_sha",
            "review_id",
            "delivery_decision",
            "reason",
        }:
            raise ValueError("Incomplete final decision")
        if (
            payload["delivery_decision"] not in ("APPROVED", "BLOCKED")
            or not isinstance(payload["reason"], str)
            or not payload["reason"]
        ):
            raise ValueError("Invalid final decision")
        for key in ("video_sha", "content_hash", "quality_sha"):
            if not isinstance(payload[key], str) or not re.fullmatch("[0-9a-f]{64}", payload[key]):
                raise ValueError("Invalid final digest")
    return True


def evaluate_final(repo, job, token, attempt, result):
    """Explicit evaluation/re-evaluation, not execution or delivery replay."""
    run = uuid.uuid4().hex
    started = time.monotonic()
    problem = None
    with repo.owned_transaction(job, token, attempt=attempt) as db:
        scope(repo, db, job)
        initialize(db)
        from .agent_contract_map import validate_payload

        try:
            payload = final_input(db, job, attempt, result, repo.clock())
            validate_payload("final", "input", payload)
            decision = decide(payload)
            validate_payload("final", "output", decision)
        except (ValueError, TypeError, KeyError) as error:
            problem = error
            payload = {"job_id": job, "attempt": attempt}
            decision = {"delivery_decision": "BLOCKED", "reason": "CONTRACT_FINAL_INPUT_INVALID"}
        db.execute(
            "INSERT OR REPLACE INTO owned_agent_contracts(job_id,attempt,role,run,plan_sha,input_sha,output_sha,status,output_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                job,
                attempt,
                "final",
                run,
                contract_plan()["sha256"],
                digest(payload),
                digest(decision),
                "FAILED" if problem else "SUCCEEDED",
                json.dumps(
                    {"input": payload, "decision": decision}, sort_keys=True, ensure_ascii=False
                ),
            ),
        )
    if problem:
        raise ContractViolation("CONTRACT_FINAL_INPUT_INVALID") from problem
    try:
        telemetry(repo, job, token, attempt, "final", run, "RUNNING", started)
        telemetry(repo, job, token, attempt, "final", run, "SUCCEEDED", started)
    except Exception as error:
        raise ContractReconciliationRequired(
            "FINAL_TELEMETRY_PENDING; decision durable, no send"
        ) from error
    return decision


def require_final(db, job, attempt, result, *, now=None):
    row = db.execute(
        "SELECT * FROM owned_agent_contracts WHERE job_id=? AND attempt=? AND role='final'",
        (job, attempt),
    ).fetchone()
    try:
        claim = db.execute(
            "SELECT details FROM events WHERE job_id=? AND event='job.claimed' ORDER BY id DESC LIMIT 1",
            (job,),
        ).fetchone()
        if not claim or json.loads(claim["details"]).get("attempt") != attempt:
            raise ValueError("Final attempt is stale")
        if not row or row["status"] != "SUCCEEDED" or row["plan_sha"] != contract_plan()["sha256"]:
            raise ValueError("No final decision")
        saved = json.loads(row["output_json"])
        payload = saved["input"]
        decision = saved["decision"]
        current = final_input(db, job, attempt, result, payload["evaluated_at"])
        if (
            payload != current
            or row["input_sha"] != digest(payload)
            or row["output_sha"] != digest(decision)
        ):
            raise ValueError("Final evidence changed")
        if decision != decide(current) or decision["delivery_decision"] != "APPROVED":
            raise ValueError("Final blocked")
        now = time.time() if now is None else now
        if not 0 <= now - payload["human_approval"]["reviewed_at"] <= 86400:
            raise ValueError("Human approval expired")
    except (ValueError, TypeError, KeyError, IndexError) as cause:
        raise ContractViolation("CONTRACT_FINAL_BLOCKED") from cause


def main():
    parser = argparse.ArgumentParser(description="Explicit local visual approval; never sends")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--lease-token", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--video-sha", required=True)
    parser.add_argument("--content-hash", required=True)
    parser.add_argument("--approve-human-frame", action="store_true", required=True)
    args = parser.parse_args()
    repo = Repository(Config.load(args.config))
    result = json.loads(args.result.read_text())
    if result["video_sha256"] != args.video_sha or result["content_hash"] != args.content_hash:
        raise ContractViolation("FINAL_COMMAND_BINDING_INVALID")
    record_human_frame(
        repo, args.job, args.lease_token, args.attempt, result, reviewer=args.reviewer
    )
    print(json.dumps(evaluate_final(repo, args.job, args.lease_token, args.attempt, result)))


if __name__ == "__main__":
    main()
