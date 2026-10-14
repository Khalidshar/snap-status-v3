"""Independent SQLite read-only observation. No Repository, repair or delivery."""

import argparse
import json
import math
import sqlite3
import time
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from studio_core.observability.agent_inventory import APPLICATIONS, ROLE_IDS


def inspect_runtime(database: str | Path, app: str, *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    if app not in APPLICATIONS or type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("WATCHDOG_REQUEST_INVALID")
    path = Path(database)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("WATCHDOG_DATABASE_UNSAFE")
    path = path.resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("WATCHDOG_SCHEMA_INVALID")
        if db.execute("SELECT 1 FROM jobs WHERE app != ? LIMIT 1", (app,)).fetchone():
            raise ValueError("WATCHDOG_APP_MISMATCH")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        incidents = []
        for row in db.execute(
            "SELECT id,state,lease_until,expires_at FROM jobs WHERE state NOT IN "
            "('COMPLETED','FAILED','EXPIRED','CANCELLED')"
        ):
            if row["expires_at"] <= now or (
                row["state"] != "QUEUED"
                and (row["lease_until"] is None or row["lease_until"] <= now)
            ):
                incidents.append(
                    {
                        "kind": "JOB_DEADLINE_OR_LEASE_EXPIRED",
                        "job_id": row["id"],
                        "state": "UNKNOWN",
                        "delivery_uncertain": row["state"] == "DELIVERING",
                    }
                )
        for row in db.execute("SELECT id FROM jobs WHERE error_code='DELIVERY_UNKNOWN'"):
            incidents.append(
                {
                    "kind": "DELIVERY_UNKNOWN",
                    "job_id": row["id"],
                    "state": "UNKNOWN",
                    "delivery_uncertain": True,
                }
            )
        roles = {
            role: {
                "instance_id": app + "/" + role,
                "role_id": role,
                "last_outcome": "NOT_OBSERVED",
                "runtime_health": "UNKNOWN",
                "function_acceptance": "NOT_VERIFIED",
            }
            for role in ROLE_IDS
        }
        if "agent_runs" in tables:
            if db.execute("SELECT 1 FROM agent_runs WHERE app != ? LIMIT 1", (app,)).fetchone():
                raise ValueError("WATCHDOG_APP_MISMATCH")
            for row in db.execute(
                "SELECT "
                "role,job_id,attempt,status,started_at,finished_at,deadline,duration_ms,error_code "
                "FROM agent_runs ORDER BY started_at,run_id"
            ):
                if row["role"] not in roles or row["started_at"] > now:
                    raise ValueError("WATCHDOG_EVIDENCE_INVALID")
                state = row["status"]
                if state not in ("RUNNING", "SUCCEEDED", "FAILED"):
                    raise ValueError("WATCHDOG_EVIDENCE_INVALID")
                if state == "RUNNING" and row["deadline"] <= now:
                    state = "UNKNOWN_STALE"
                    incidents.append(
                        {
                            "kind": "AGENT_STALE",
                            "job_id": row["job_id"],
                            "role_id": row["role"],
                            "state": state,
                        }
                    )
                roles[row["role"]].update(
                    last_outcome=state,
                    job_id=row["job_id"],
                    attempt=row["attempt"],
                    started_at=row["started_at"],
                    finished_at=row["finished_at"],
                    duration_ms=row["duration_ms"],
                    error_code=row["error_code"],
                )
        locks = []
        if "component_locks" in tables:
            if db.execute(
                "SELECT 1 FROM component_locks WHERE app != ? LIMIT 1", (app,)
            ).fetchone():
                raise ValueError("WATCHDOG_APP_MISMATCH")
            for row in db.execute("SELECT component,purpose,deadline FROM component_locks"):
                state = "ACTIVE" if row["deadline"] > now else "UNKNOWN_STALE"
                locks.append(
                    {"component": row["component"], "purpose": row["purpose"], "state": state}
                )
                if state == "UNKNOWN_STALE":
                    incidents.append(
                        {"kind": "COMPONENT_STALE", "component": row["component"], "state": state}
                    )
        return {
            "schema": 1,
            "app": app,
            "collected_at": now,
            "source": "OWNED_SQLITE_READ_ONLY",
            "agents": list(roles.values()),
            "component_locks": locks,
            "incidents": incidents,
            "automatic_repair": False,
            "delivery_authority": False,
            "production_ready": False,
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--app", required=True, choices=APPLICATIONS)
    args = parser.parse_args(argv)
    try:
        result = inspect_runtime(args.database, args.app)
    except (OSError, ValueError, sqlite3.Error):
        print(json.dumps({"error": "WATCHDOG_UNAVAILABLE", "runtime_health": "UNKNOWN"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 1 if result["incidents"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
