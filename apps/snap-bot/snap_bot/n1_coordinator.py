"""Opt-in DB authority coordinator; DB fencing precedes physical sealing.
Existing worker/renderer/delivery services do not enable this module.
"""

import json
import sqlite3
import uuid
from typing import Any

from .n1_contracts import Ledger, Refused, canonical
from .n1_writer import generation


class JournalCoordinator:
    def __init__(self, ledger: Ledger, control: Any) -> None:
        self.ledger = ledger
        self.repo = ledger.repo
        self.control = control
        with self.repo.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS n1_control(gen TEXT PRIMARY KEY,parent TEXT,descriptor TEXT NOT NULL,confirmed INTEGER NOT NULL CHECK(confirmed IN (0,1)))"
            )

    def _prepare(self, db: sqlite3.Connection, segment: Any, parent: str | None) -> Any:
        desc = generation(
            {
                "type": "segment",
                "app": self.repo.config.app,
                "gen": segment["gen"],
                "parent": parent,
                "base_seq": segment["seq"],
                "base_digest": segment["digest"],
            },
            self.ledger.key,
        )
        db.execute(
            "INSERT INTO n1_control VALUES(?,?,?,0)", (segment["gen"], parent, canonical(desc))
        )
        return db.execute("SELECT * FROM n1_control WHERE gen=?", (segment["gen"],)).fetchone()

    def ensure_current(self, job: str, token: str, attempt: int) -> Any:
        with self.repo.owned_transaction(job, token, attempt=attempt) as db:
            segment = db.execute("SELECT * FROM n1_segments WHERE state='ACTIVE'").fetchone()
            if not segment:
                raise Refused("CONTROL_NO_ACTIVE")
            row = db.execute("SELECT * FROM n1_control WHERE gen=?", (segment["gen"],)).fetchone()
            if row is None:
                if (
                    db.execute("SELECT COUNT(*) FROM n1_segments").fetchone()[0] != 1
                    or segment["seq"] != 0
                ):
                    raise Refused("CONTROL_HISTORY_MISSING")
                row = self._prepare(db, segment, None)
            saved = dict(row)
        desc = json.loads(saved["descriptor"])
        if not saved["confirmed"]:
            result = (
                self.control.register(desc)
                if saved["parent"] is None
                else self.control.seal(saved["parent"], desc)
            )
            expected = "ACTIVE" if saved["parent"] is None else "SEALED"
            if result != {"gen": saved["gen"], "state": expected}:
                raise Refused("CONTROL_ACK_BINDING")
            with self.repo.owned_transaction(job, token, attempt=attempt) as db:
                active = db.execute("SELECT gen FROM n1_segments WHERE state='ACTIVE'").fetchone()
                if not active or active["gen"] != saved["gen"]:
                    raise Refused("CONTROL_ACTIVE_CHANGED")
                db.execute(
                    "UPDATE n1_control SET confirmed=1 WHERE gen=? AND descriptor=?",
                    (saved["gen"], saved["descriptor"]),
                )
        return desc

    def rotate(self, job: str, token: str, attempt: int) -> Any:
        current = self.ensure_current(job, token, attempt)
        with self.repo.owned_transaction(job, token, attempt=attempt) as db:
            old = db.execute("SELECT * FROM n1_segments WHERE state='ACTIVE'").fetchone()
            if not old or old["gen"] != current["body"]["gen"]:
                raise Refused("CONTROL_ACTIVE_CHANGED")
            # Logical fence and durable pending command commit atomically.
            db.execute("UPDATE n1_segments SET state='SEALED' WHERE gen=?", (old["gen"],))
            db.execute(
                "UPDATE n1_ledger SET state='UNKNOWN' WHERE gen=? AND state='PENDING'",
                (old["gen"],),
            )
            new = uuid.uuid4().hex
            db.execute(
                "INSERT INTO n1_segments VALUES(?,'ACTIVE',?,?)", (new, old["seq"], old["digest"])
            )
            self._prepare(db, dict(gen=new, seq=old["seq"], digest=old["digest"]), old["gen"])
        # If this fails/loses its reply, ensure_current retries the same stored child.
        return self.ensure_current(job, token, attempt)

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
        desc = self.ensure_current(job, token, attempt)
        envelope = self.ledger.begin(job, token, attempt, agent, status, event_id, payload_sha)
        if envelope["body"]["gen"] != desc["body"]["gen"]:
            raise Refused("CONTROL_GENERATION_CHANGED")
        return {"generation": desc, "envelope": envelope}
