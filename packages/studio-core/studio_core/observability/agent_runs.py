"""Owned computation evidence. No retries, delivery, or acceptance inference.

Callbacks remain subject to their worker's process/systemd timeout and network
sandbox. A local budget rejects a late result; it cannot stop arbitrary Python.
The durable start precedes the callback, so a crash cannot cause an implicit
replay. Output payloads and exception text are never saved in this ledger.
"""

import contextlib
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from studio_core.jobs import LeaseLost
from studio_core.jobs.repository import Repository
from studio_core.observability.agent_inventory import APPLICATIONS, ROLE_IDS


class AgentBlocked(RuntimeError):
    pass


class AgentRejected(ValueError):
    pass


class AgentBudgetExceeded(TimeoutError):
    pass


def encoded(value: Any) -> bytes:
    try:
        raw = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise AgentRejected("AGENT_PAYLOAD_INVALID") from None
    if len(raw) > 1024 * 1024:
        raise AgentRejected("AGENT_PAYLOAD_TOO_LARGE")
    return raw


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def initialize(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS agent_runs(
        app TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id),
        attempt INTEGER NOT NULL, role TEXT NOT NULL, run_id TEXT NOT NULL,
        version TEXT NOT NULL, contract_sha TEXT NOT NULL,
        input_sha TEXT NOT NULL, output_sha TEXT,
        started_at REAL NOT NULL, finished_at REAL, deadline REAL NOT NULL,
        duration_ms INTEGER, status TEXT NOT NULL, error_code TEXT,
        PRIMARY KEY(app,job_id,attempt,role))""")


@dataclass(frozen=True)
class AgentContract:
    app: str
    command: str
    role: str
    version: str
    contract_sha: str
    dependencies: tuple[str, ...] = ()
    budget_seconds: float = 30

    def __post_init__(self) -> None:
        if (
            self.app not in APPLICATIONS
            or self.role not in ROLE_IDS
            or not isinstance(self.command, str)
            or not re.fullmatch("[a-z][a-z0-9_]{0,63}", self.command)
            or not isinstance(self.version, str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.version)
            or not isinstance(self.contract_sha, str)
            or not re.fullmatch("[0-9a-f]{64}", self.contract_sha)
        ):
            raise AgentRejected("AGENT_CONTRACT_INVALID")
        if (
            not isinstance(self.dependencies, tuple)
            or any(role not in ROLE_IDS or role == self.role for role in self.dependencies)
            or len(set(self.dependencies)) != len(self.dependencies)
        ):
            raise AgentRejected("AGENT_DEPENDENCIES_INVALID")
        if (
            type(self.budget_seconds) not in (int, float)
            or not math.isfinite(self.budget_seconds)
            or not 0 < self.budget_seconds <= 1200
        ):
            raise AgentRejected("AGENT_BUDGET_INVALID")


def _scope(
    repo: Repository,
    db: sqlite3.Connection,
    job: str,
    token: str,
    attempt: int,
    contract: AgentContract,
) -> dict[str, Any]:
    owned = repo.assert_owner(job, token, attempt=attempt, db=db)
    if db.execute("SELECT 1 FROM jobs WHERE app != ? LIMIT 1", (contract.app,)).fetchone():
        raise AgentRejected("AGENT_DATABASE_SCOPE_INVALID")
    if (
        repo.config.app != contract.app
        or owned["app"] != contract.app
        or owned["command"] != contract.command
        or owned["state"] == "DELIVERING"
    ):
        raise AgentRejected("AGENT_SCOPE_INVALID")
    return owned


def _finish(
    repo: Repository,
    job: str,
    token: str,
    attempt: int,
    contract: AgentContract,
    run: str,
    started: float,
    status: str,
    output_sha: str | None = None,
    code: str | None = None,
) -> None:
    duration = max(0, int((time.monotonic() - started) * 1000))
    with repo.owned_transaction(job, token, attempt=attempt) as db:
        _scope(repo, db, job, token, attempt, contract)
        row = db.execute(
            "SELECT deadline FROM agent_runs WHERE app=? AND job_id=? AND attempt=? AND role=? "
            "AND run_id=? AND status=?",
            (contract.app, job, attempt, contract.role, run, "RUNNING"),
        ).fetchone()
        if (
            status == "SUCCEEDED"
            and row
            and (
                repo.clock() >= row["deadline"]
                or time.monotonic() - started > contract.budget_seconds
            )
        ):
            raise AgentBudgetExceeded("AGENT_BUDGET_EXCEEDED")
        result = db.execute(
            """UPDATE agent_runs SET status=?, output_sha=?,
            finished_at=?, duration_ms=?, error_code=? WHERE app=? AND job_id=?
            AND attempt=? AND role=? AND run_id=? AND status='RUNNING' """,
            (
                status,
                output_sha,
                repo.clock(),
                duration,
                code,
                contract.app,
                job,
                attempt,
                contract.role,
                run,
            ),
        )
        if result.rowcount != 1:
            raise AgentBlocked("AGENT_LEDGER_CHANGED")
        repo.event(
            db,
            job,
            "agent.execution",
            {
                "agent": contract.role,
                "attempt": attempt,
                "run": run,
                "status": status,
                "duration_ms": duration,
                "error_code": code,
            },
        )


def run_agent(
    repo: Repository,
    job: str,
    token: str,
    attempt: int,
    contract: AgentContract,
    payload: Any,
    callback: Callable[[], Any],
    validate_input: Callable[..., Any],
    validate_output: Callable[..., Any],
    *,
    bind_dependency: bool = True,
) -> Any:
    """One owned invocation per role/attempt; validators must return True.

    Successful dependency hashes bind the input by default. Multiple-dependency
    contracts can explicitly opt out of hash equality while keeping their
    success gate, and must then validate all lineage in validate_input.
    Validation occurs before any callback. No implicit retries or adoption.
    """
    if not isinstance(contract, AgentContract):
        raise AgentRejected("AGENT_CONTRACT_INVALID")
    input_sha = digest(payload)
    repo.assert_owner(job, token, attempt=attempt)
    try:
        valid = validate_input(payload) is True
    except Exception:
        valid = False
    if not valid:
        raise AgentRejected("AGENT_INPUT_INVALID") from None
    if digest(payload) != input_sha:
        raise AgentRejected("AGENT_INPUT_CHANGED")
    run = uuid.uuid4().hex
    with repo.owned_transaction(job, token, attempt=attempt) as db:
        owned = _scope(repo, db, job, token, attempt, contract)
        initialize(db)
        if db.execute(
            "SELECT 1 FROM agent_runs WHERE app=? AND job_id=? AND attempt=? AND role=?",
            (contract.app, job, attempt, contract.role),
        ).fetchone():
            raise AgentBlocked("AGENT_REPLAY_REQUIRES_RECONCILIATION")
        for role in contract.dependencies:
            dependency = db.execute(
                "SELECT status,output_sha,contract_sha FROM agent_runs WHERE app=? AND job_id=? "
                "AND attempt=? AND role=?",
                (contract.app, job, attempt, role),
            ).fetchone()
            if (
                not dependency
                or dependency["status"] != "SUCCEEDED"
                or dependency["contract_sha"] != contract.contract_sha
                or (bind_dependency and dependency["output_sha"] != input_sha)
            ):
                raise AgentBlocked("AGENT_UPSTREAM_BLOCKED")
        now = repo.clock()
        deadline = min(now + contract.budget_seconds, owned["expires_at"], owned["lease_until"])
        db.execute(
            "INSERT INTO agent_runs VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,NULL,?,NULL)",
            (
                contract.app,
                job,
                attempt,
                contract.role,
                run,
                contract.version,
                contract.contract_sha,
                input_sha,
                None,
                now,
                deadline,
                "RUNNING",
            ),
        )
        repo.event(
            db,
            job,
            "agent.execution",
            {
                "agent": contract.role,
                "attempt": attempt,
                "run": run,
                "status": "RUNNING",
                "version": contract.version,
                "input_sha": input_sha,
                "contract_sha": contract.contract_sha,
            },
        )
    started = time.monotonic()
    try:
        value = callback()
        if digest(payload) != input_sha:
            raise AgentRejected("AGENT_INPUT_CHANGED")
        if time.monotonic() - started > contract.budget_seconds or repo.clock() >= deadline:
            raise AgentBudgetExceeded("AGENT_BUDGET_EXCEEDED")
        try:
            valid = validate_output(value, payload) is True
        except Exception:
            valid = False
        if not valid:
            raise AgentRejected("AGENT_OUTPUT_INVALID")
        output_sha = digest(value)
        _finish(
            repo, job, token, attempt, contract, run, started, "SUCCEEDED", output_sha=output_sha
        )
    except BaseException as error:
        # A stale owner cannot record success or rewrite the new epoch. The
        # durable RUNNING record remains unknown until independently inspected.
        code = (
            "AGENT_BUDGET_EXCEEDED"
            if isinstance(error, AgentBudgetExceeded)
            else "AGENT_OUTPUT_INVALID"
            if isinstance(error, AgentRejected)
            else "AGENT_CALLBACK_FAILED"
        )
        with contextlib.suppress(LeaseLost, AgentBlocked):
            _finish(repo, job, token, attempt, contract, run, started, "FAILED", code=code)
        raise
    return value
