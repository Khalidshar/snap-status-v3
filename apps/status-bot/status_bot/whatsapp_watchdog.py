"""Independent read-only WhatsApp outbox snapshot; no keys, control or network."""

import argparse
import json
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .whatsapp_contract import finite, identifier, require

STATES = {
    "PREPARED",
    "UPLOADING",
    "SEND_INTENT",
    "API_ACCEPTED",
    "SENT",
    "DELIVERED",
    "READ",
    "FAILED_REMOTE",
    "REJECTED",
    "UPLOAD_UNKNOWN",
    "DELIVERY_UNKNOWN",
}


def inspect_outbox(database: str | Path, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    require(finite(now), "WA_OBSERVATION_UNAVAILABLE")
    database = Path(database).absolute()
    require(database.is_file() and not database.is_symlink(), "WA_OBSERVATION_UNAVAILABLE")
    db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        require(
            not db.execute("SELECT 1 FROM jobs WHERE app!='status-bot-v2' LIMIT 1").fetchone(),
            "WA_OBSERVATION_SCOPE_INVALID",
        )
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='whatsapp_outbox'"
        ).fetchone()
        rows: list[dict[str, Any]] = []
        count = 0
        if exists:
            count = db.execute("SELECT count(*) FROM whatsapp_outbox").fetchone()[0]
            for row in db.execute(
                "SELECT id,job_id,attempt,state,expires_at,error_code FROM whatsapp_outbox "
                "WHERE app='status-bot-v2' ORDER BY created_at,id LIMIT 1000"
            ):
                state = row["state"] if row["state"] in STATES else "UNKNOWN"
                health = (
                    "CONFIRMED"
                    if state in ("DELIVERED", "READ")
                    else "UNKNOWN"
                    if state in ("UPLOAD_UNKNOWN", "DELIVERY_UNKNOWN", "UNKNOWN")
                    or (row["expires_at"] <= now and state not in ("REJECTED", "FAILED_REMOTE"))
                    else "BLOCKED"
                    if state in ("REJECTED", "FAILED_REMOTE")
                    else "PENDING"
                )
                code = row["error_code"]
                if code is not None and not identifier(
                    code, "WA_(?:API_REJECTED_[0-9]{1,7}|UPLOAD_UNKNOWN|DELIVERY_UNKNOWN)"
                ):
                    code = "WA_UNKNOWN"
                rows.append(
                    {
                        "outbox_id": row["id"]
                        if identifier(row["id"], "[0-9a-f]{32}")
                        else "UNKNOWN",
                        "job_id": row["job_id"]
                        if identifier(row["job_id"], "[0-9a-f]{32}")
                        else "UNKNOWN",
                        "attempt": row["attempt"]
                        if type(row["attempt"]) is int and row["attempt"] > 0
                        else None,
                        "state": state,
                        "observation": health,
                        "error_code": code,
                        "publication": "MANUAL_PUBLICATION_PENDING",
                    }
                )
        return {
            "app": "status-bot-v2",
            "observed_at": now,
            "read_only": True,
            "database_available": True,
            "outbox_present": bool(exists),
            "items": rows,
            "total_items": count,
            "truncated": count > 1000,
            "result": "UNKNOWN"
            if not exists or count > 1000 or any(r["observation"] == "UNKNOWN" for r in rows)
            else "OBSERVED",
            "delivery_authority": False,
            "production_acceptance": "NOT_VERIFIED",
        }
    finally:
        db.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = inspect_outbox(args.database)
    except Exception:
        print(
            json.dumps(
                {
                    "app": "status-bot-v2",
                    "result": "UNKNOWN",
                    "read_only": True,
                    "error_code": "WA_OBSERVATION_UNAVAILABLE",
                    "delivery_authority": False,
                }
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result["result"] == "UNKNOWN" else 0


if __name__ == "__main__":
    raise SystemExit(main())
