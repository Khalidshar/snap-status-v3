"""P0 reference contracts only. Not wired to services, rendering, or replay.
Append receipts below model an authenticated writer response, NOT filesystem durability.
"""

import hashlib
import hmac
import json
import re
import uuid
from typing import Any

from studio_core.jobs import LeaseLost, Repository


class Refused(RuntimeError):
    pass


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def recovery_gate(
    repo: Repository, job: str, token: str, attempt: int, *, enabled: bool = False
) -> bool:
    """Read the entire authoritative history in the same owner transaction."""
    with repo.owned_transaction(job, token, attempt=attempt) as db:
        rows = db.execute(
            "SELECT event,details FROM events WHERE job_id=? ORDER BY id", (job,)
        ).fetchall()
        claims: list[dict[str, Any]] = []
        phases: list[str] = []
        try:
            for row in rows:
                details = json.loads(row["details"])
                if row["event"] == "job.claimed":
                    claims.append(details)
                    if details.get("recovered_from") is not None:
                        phases.append(details["recovered_from"])
                elif row["event"] == "job.state":
                    phases.append(details["state"])
        except (ValueError, KeyError, TypeError):
            raise Refused("RECOVERY_HISTORY_INVALID") from None
        if len(claims) != attempt or any(c.get("attempt") != i + 1 for i, c in enumerate(claims)):
            raise Refused("RECOVERY_HISTORY_INVALID")
        if attempt > 3:
            raise Refused("RECOVERY_ATTEMPTS_EXHAUSTED")
        if attempt == 1:
            return True
        if not enabled:
            raise Refused("RECOVERY_DISABLED")
        if claims[-1].get("recovered_from") not in {"RUNNING", "RESEARCHING"}:
            raise Refused("RECOVERY_REFUSED")
        if any(s not in {"QUEUED", "RUNNING", "RESEARCHING"} for s in phases):
            raise Refused("RECOVERY_REFUSED")
        # These contracts are deliberately stricter than eventual stage adoption.
        if db.execute("SELECT 1 FROM deliveries WHERE job_id=? LIMIT 1", (job,)).fetchone():
            raise Refused("RECOVERY_REFUSED")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if (
            "telegram_outbox" in tables
            and db.execute(
                "SELECT 1 FROM telegram_outbox WHERE job_id=? LIMIT 1", (job,)
            ).fetchone()
        ):
            raise Refused("RECOVERY_REFUSED")
        if (
            "stage_results" in tables
            and db.execute(
                "SELECT 1 FROM stage_results WHERE job_id=? AND stage IN ('design','quality') LIMIT 1",
                (job,),
            ).fetchone()
        ):
            raise Refused("RECOVERY_REFUSED")
        if (
            "n1_ledger" in tables
            and db.execute(
                "SELECT 1 FROM n1_ledger WHERE job_id=? AND agent IN ('design','quality') LIMIT 1",
                (job,),
            ).fetchone()
        ):
            raise Refused("RECOVERY_REFUSED")
        return True


class Ledger:
    """Application-local SQLite authority model. No append/fsync under a DB lock."""

    def __init__(self, repo: Repository, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("Synthetic key too short")
        self.repo, self.key = repo, key
        with repo.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS n1_segments(gen TEXT PRIMARY KEY,state TEXT NOT NULL CHECK(state IN ('ACTIVE','SEALED')),seq INTEGER NOT NULL,digest TEXT NOT NULL)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS n1_one_active ON n1_segments(state) WHERE state='ACTIVE'"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS n1_ledger(intent TEXT PRIMARY KEY,job_id TEXT NOT NULL,attempt INTEGER NOT NULL,agent TEXT NOT NULL,event_id TEXT NOT NULL,gen TEXT NOT NULL REFERENCES n1_segments(gen),state TEXT NOT NULL CHECK(state IN ('PENDING','APPENDED','UNKNOWN')),body TEXT NOT NULL,mac TEXT NOT NULL,receipt TEXT,UNIQUE(job_id,attempt,agent,event_id))"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS n1_one_pending ON n1_ledger(gen) WHERE state='PENDING'"
            )
            if not db.execute("SELECT 1 FROM n1_segments WHERE state='ACTIVE'").fetchone():
                # Only initial construction creates a segment; rotation is explicit.
                if not db.execute("SELECT 1 FROM n1_segments").fetchone():
                    db.execute(
                        "INSERT INTO n1_segments VALUES(?,'ACTIVE',0,?)",
                        (uuid.uuid4().hex, "0" * 64),
                    )

    def mac(self, domain: str, value: object) -> str:
        return hmac.new(
            self.key, (domain + "\n" + canonical(value)).encode(), hashlib.sha256
        ).hexdigest()

    def begin(
        self,
        job: str,
        token: str,
        attempt: int,
        agent: str,
        status: str,
        event_id: str,
        payload_sha: str,
    ) -> dict[str, Any]:
        if agent not in {"fact_source", "design", "quality"} or status not in {
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
        }:
            raise ValueError("Invalid stage event")
        if not re.fullmatch("[a-zA-Z0-9_-]{1,64}", event_id) or not re.fullmatch(
            "[0-9a-f]{64}", payload_sha
        ):
            raise ValueError("Invalid event identity")
        with self.repo.owned_transaction(job, token, attempt=attempt) as db:
            owned = self.repo.assert_owner(job, token, attempt=attempt, db=db)
            prior = db.execute(
                "SELECT * FROM n1_ledger WHERE job_id=? AND attempt=? AND agent=? AND event_id=?",
                (job, attempt, agent, event_id),
            ).fetchone()
            if prior:
                body = json.loads(prior["body"])
                if body["status"] != status or body["payload_sha256"] != payload_sha:
                    raise Refused("INTENT_CONFLICT")
                if prior["state"] == "UNKNOWN":
                    raise Refused("APPEND_UNKNOWN")
                return {"body": body, "mac": prior["mac"]}
            segment = db.execute("SELECT * FROM n1_segments WHERE state='ACTIVE'").fetchone()
            if not segment:
                raise Refused("JOURNAL_UNAVAILABLE")
            if db.execute(
                "SELECT 1 FROM n1_ledger WHERE gen=? AND state='PENDING'", (segment["gen"],)
            ).fetchone():
                raise Refused("JOURNAL_BUSY")
            body = {
                "app": self.repo.config.app,
                "job_id": job,
                "attempt": attempt,
                "agent": agent,
                "status": status,
                "event_id": event_id,
                "intent_id": uuid.uuid4().hex,
                "owner_fp": self.mac("owner", {"token": token}),
                "payload_sha256": payload_sha,
                "not_after": min(owned["lease_until"], owned["expires_at"]),
                "kid": "p0-fixture-v1",
                "gen": segment["gen"],
                "seq": segment["seq"] + 1,
                "prev": segment["digest"],
            }
            mac = self.mac("n1.intent.v1", body)
            db.execute(
                "INSERT INTO n1_ledger VALUES(?,?,?,?,?,?,'PENDING',?,?,NULL)",
                (
                    body["intent_id"],
                    job,
                    attempt,
                    agent,
                    event_id,
                    segment["gen"],
                    canonical(body),
                    mac,
                ),
            )
            return {"body": body, "mac": mac}

    def simulated_writer_receipt(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Test model only; Linux broker must actually append+fsync before replying."""
        if set(envelope) != {"body", "mac"} or not hmac.compare_digest(
            self.mac("n1.intent.v1", envelope["body"]), envelope["mac"]
        ):
            raise Refused("INTENT_MAC_INVALID")
        body = envelope["body"]
        if self.repo.clock() >= body["not_after"]:
            raise Refused("INTENT_EXPIRED")
        receipt: dict[str, Any] = {
            "envelope": envelope,
            "digest": sha(body["prev"] + "\n" + canonical(envelope)),
        }
        receipt["proof"] = self.mac("n1.receipt.v1", receipt)
        return receipt

    def confirm(
        self, job: str, token: str, attempt: int, receipt: dict[str, Any]
    ) -> dict[str, Any]:
        if set(receipt) != {"envelope", "digest", "proof"}:
            raise Refused("RECEIPT_INVALID")
        unsigned = {k: receipt[k] for k in ("envelope", "digest")}
        if not hmac.compare_digest(self.mac("n1.receipt.v1", unsigned), receipt["proof"]):
            raise Refused("RECEIPT_MAC_INVALID")
        envelope = receipt["envelope"]
        body = envelope["body"]
        with self.repo.owned_transaction(job, token, attempt=attempt) as db:
            self.repo.assert_owner(job, token, attempt=attempt, db=db)
            row = db.execute(
                "SELECT * FROM n1_ledger WHERE intent=?", (body["intent_id"],)
            ).fetchone()
            if (
                not row
                or row["job_id"] != job
                or row["attempt"] != attempt
                or row["body"] != canonical(body)
                or row["mac"] != envelope["mac"]
            ):
                raise Refused("JOURNAL_LEDGER_MISMATCH")
            if (
                body["owner_fp"] != self.mac("owner", {"token": token})
                or self.repo.clock() >= body["not_after"]
            ):
                raise LeaseLost("Expired intent owner")
            segment = db.execute("SELECT * FROM n1_segments WHERE gen=?", (row["gen"],)).fetchone()
            if row["state"] == "APPENDED":
                if row["receipt"] != canonical(receipt):
                    raise Refused("RECEIPT_CONFLICT")
                return receipt
            if row["state"] != "PENDING" or segment["state"] != "ACTIVE":
                raise Refused("SEGMENT_SEALED")
            if body["seq"] != segment["seq"] + 1 or body["prev"] != segment["digest"]:
                raise Refused("JOURNAL_CAS_FAILED")
            if receipt["digest"] != sha(body["prev"] + "\n" + canonical(envelope)):
                raise Refused("JOURNAL_DIGEST_INVALID")
            db.execute(
                "UPDATE n1_ledger SET state='APPENDED',receipt=? WHERE intent=?",
                (canonical(receipt), row["intent"]),
            )
            db.execute(
                "UPDATE n1_segments SET seq=?,digest=? WHERE gen=?",
                (body["seq"], receipt["digest"], row["gen"]),
            )
            return receipt

    def seal(self, gen: str) -> str | None:
        """Atomic logical fencing. This does not delete locks or prove a writer dead."""
        with self.repo.transaction() as db:
            segment = db.execute("SELECT * FROM n1_segments WHERE gen=?", (gen,)).fetchone()
            if not segment:
                raise Refused("UNKNOWN_SEGMENT")
            if segment["state"] == "SEALED":
                return None
            db.execute("UPDATE n1_segments SET state='SEALED' WHERE gen=?", (gen,))
            db.execute(
                "UPDATE n1_ledger SET state='UNKNOWN' WHERE gen=? AND state='PENDING'", (gen,)
            )
            new = uuid.uuid4().hex
            # Logical anchor only. Physical-segment manifest/crash protocol is P1+.
            db.execute(
                "INSERT INTO n1_segments VALUES(?,'ACTIVE',?,?)",
                (new, segment["seq"], segment["digest"]),
            )
            return new

    def view(self, job: str) -> list[Any]:
        with self.repo.connect() as db:
            db.execute("BEGIN")
            epoch = db.execute(
                "SELECT COUNT(*) FROM events WHERE job_id=? AND event='job.claimed'", (job,)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT receipt FROM n1_ledger WHERE job_id=? AND attempt=? AND state='APPENDED' ORDER BY rowid",
                (job, epoch),
            ).fetchall()
            return [json.loads(r["receipt"]) for r in rows]
