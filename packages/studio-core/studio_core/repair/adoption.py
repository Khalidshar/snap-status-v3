"""Fail expired local adoption waits; never reclaim, render, or deliver.

Graduated from the lab (snap_app.adoption_pending) with types added; the
behaviour is unchanged.
"""

import hashlib
import json
import sqlite3
from typing import Any

from studio_core.jobs.repository import LeaseLost, Repository


def claim_attempt(details: dict[str, Any], legacy_attempt: int) -> int:
    """Explicit generation wins; counting is only a legacy missing-field fallback."""
    attempt = details.get("attempt", legacy_attempt)
    if type(attempt) is not int or attempt < 1:
        raise ValueError("Invalid claim attempt history")
    return attempt


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS render_adoption_pending(job_id TEXT PRIMARY KEY,"
        "attempt INTEGER NOT NULL,owner_hash TEXT NOT NULL,lease_until REAL NOT NULL)"
    )


def mark_pending(repo: Repository, job: str, token: str, attempt: int) -> None:
    # Failure bookkeeping may arrive at the expiry boundary. Match the exact
    # generation/token under the lock; no resource adoption or send is allowed.
    with repo.transaction() as db:
        owned = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
        claims = db.execute(
            "SELECT details FROM events WHERE job_id=? AND event='job.claimed' ORDER BY id", (job,)
        ).fetchall()
        epoch = claim_attempt(json.loads(claims[-1]["details"]) if claims else {}, len(claims))
        if (
            not owned
            or owned["state"] != "RENDERING"
            or owned["lease_token"] != token
            or type(attempt) is not int
            or epoch != attempt
        ):
            raise LeaseLost("Pending marker owner changed")
        initialize(db)
        db.execute(
            "INSERT INTO render_adoption_pending(job_id,attempt,owner_hash,lease_until) "
            "VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET attempt=excluded.attempt,"
            "owner_hash=excluded.owner_hash,lease_until=excluded.lease_until",
            (job, attempt, hashlib.sha256(token.encode()).hexdigest(), owned["lease_until"]),
        )
        repo.event(
            db, job, "job.adoption_pending", {"attempt": attempt, "code": "RENDER_ADOPTION_PENDING"}
        )


def clear_pending(db: sqlite3.Connection, job: str) -> None:
    initialize(db)
    db.execute("DELETE FROM render_adoption_pending WHERE job_id=?", (job,))


def expire_pending_adoptions(repo: Repository, limit: int = 100) -> list[str]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid expiry batch")
    failed = []
    with repo.transaction() as db:
        initialize(db)
        now = repo.clock()
        rows = db.execute(
            "SELECT p.*,j.state,j.lease_token,j.lease_until AS current_until "
            "FROM render_adoption_pending p JOIN jobs j ON j.id=p.job_id "
            "WHERE (j.state='RENDERING' AND j.lease_until<=?) "
            "OR (j.state='RUNNING' AND p.lease_until<=?) "
            "ORDER BY p.lease_until,p.job_id LIMIT ?",
            (now, now, limit),
        ).fetchall()
        for row in rows:
            claims = db.execute(
                "SELECT details FROM events WHERE job_id=? AND event='job.claimed' ORDER BY id",
                (row["job_id"],),
            ).fetchall()
            latest = json.loads(claims[-1]["details"]) if claims else {}
            epoch = claim_attempt(latest, len(claims))
            same = (
                row["state"] == "RENDERING"
                and epoch == row["attempt"]
                and hashlib.sha256((row["lease_token"] or "").encode()).hexdigest()
                == row["owner_hash"]
            )
            # Cover a claim racing the pre-claim sweep, before any new work starts.
            reclaimed = (
                row["state"] == "RUNNING"
                and type(epoch) is int
                and epoch > row["attempt"]
                and latest.get("recovered_from") == "RENDERING"
            )
            if not (same or reclaimed):
                continue
            # Any delivery evidence belongs to reconciliation, never this policy.
            if db.execute(
                "SELECT 1 FROM deliveries WHERE job_id=? LIMIT 1", (row["job_id"],)
            ).fetchone():
                continue
            db.execute(
                "UPDATE jobs SET state='FAILED',error_code='ADOPTION_PENDING_EXPIRED',"
                "updated_at=?,lease_token=NULL,lease_until=NULL WHERE id=?",
                (now, row["job_id"]),
            )
            repo.event(
                db,
                row["job_id"],
                "job.adoption_pending_expired",
                {"attempt": row["attempt"], "code": "ADOPTION_PENDING_EXPIRED"},
            )
            clear_pending(db, row["job_id"])
            failed.append(row["job_id"])
    return failed
