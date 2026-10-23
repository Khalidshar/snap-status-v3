"""P0-C physical segment reference. No live broker or systemd integration.
Only a trusted, append-only writer is modeled. Same-UID hostile writers are not isolated.
"""

import json
import os
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from studio_core.jobs import Repository

from .n1_artifacts import projection_lock, sync_directory
from .n1_contracts import Ledger, Refused, canonical, sha


class SegmentCorrupt(RuntimeError):
    pass


class JournalSegments:
    LIMIT = 2_000_000

    def __init__(self, repo: Repository, key: bytes, root: str | os.PathLike[str]) -> None:
        self.repo = repo
        self.ledger = Ledger(repo, key)
        self.root = Path(root)
        if self.root.is_symlink():
            raise ValueError("Symlink journal root")
        self.root.mkdir(parents=True, exist_ok=True)
        with repo.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS n1_segment_files(gen TEXT PRIMARY KEY REFERENCES n1_segments(gen),phase TEXT NOT NULL CHECK(phase IN ('PREPARING','READY','SEALED')),parent TEXT,base_seq INTEGER NOT NULL,base_digest TEXT NOT NULL,seal_seq INTEGER,seal_digest TEXT)"
            )
            rows = db.execute("SELECT * FROM n1_segments ORDER BY rowid").fetchall()
            # Only brand-new ledgers can acquire initial physical metadata implicitly.
            if not db.execute("SELECT 1 FROM n1_segment_files").fetchone():
                if len(rows) != 1 or rows[0]["seq"] != 0:
                    raise Refused("PHYSICAL_MIGRATION_REQUIRED")
                db.execute(
                    "INSERT INTO n1_segment_files VALUES(?,'PREPARING',NULL,0,?,NULL,NULL)",
                    (rows[0]["gen"], "0" * 64),
                )
        self.recover()

    def metadata(self, gen: str) -> dict[str, Any]:
        with self.repo.connect() as db:
            row = db.execute(
                "SELECT f.*,s.state FROM n1_segment_files f JOIN n1_segments s USING(gen) WHERE gen=?",
                (gen,),
            ).fetchone()
            if not row:
                raise Refused("UNKNOWN_PHYSICAL_SEGMENT")
            return dict(row)

    def path(self, gen: str) -> Path:
        if len(gen) != 32 or any(c not in "0123456789abcdef" for c in gen):
            raise ValueError("Invalid generation")
        return self.root / ("events-" + gen + ".jsonl")

    def header(self, meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "segment",
            "app": self.repo.config.app,
            "gen": meta["gen"],
            "parent": meta["parent"],
            "base_seq": meta["base_seq"],
            "base_digest": meta["base_digest"],
        }

    def _prepare(self, gen: str, failpoint: Callable[[str], object] | None = None) -> None:
        meta = self.metadata(gen)
        path = self.path(gen)
        header = (canonical(self.header(meta)) + "\n").encode()
        fd, name = tempfile.mkstemp(dir=self.root, prefix=".segment-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(header)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, path)
            except FileExistsError:
                if path.is_symlink():
                    raise SegmentCorrupt("Symlink segment") from None
                with path.open("rb") as stream:
                    existing = stream.readline(4097)
                if existing != header:
                    raise SegmentCorrupt("Segment header conflict") from None
            sync_directory(self.root)
            if failpoint:
                failpoint("after_file_publish")
            with self.repo.transaction() as db:
                db.execute(
                    "UPDATE n1_segment_files SET phase='READY' WHERE gen=? AND phase='PREPARING'",
                    (gen,),
                )
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def recover(self) -> None:
        with self.repo.connect() as db:
            pending = [
                r[0] for r in db.execute("SELECT gen FROM n1_segment_files WHERE phase='PREPARING'")
            ]
        for gen in pending:
            self._prepare(gen)
        # Never delete lock files, truncate tails, or infer process death from timeout.

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
        envelope = self.ledger.begin(job, token, attempt, agent, status, event_id, payload_sha)
        meta = self.metadata(envelope["body"]["gen"])
        if meta["phase"] != "READY" or meta["state"] != "ACTIVE":
            raise Refused("SEGMENT_NOT_READY")
        return envelope

    def scan(self, gen: str) -> list[dict[str, Any]]:
        """Verify confirmed sealed prefix only; retain and ignore any trailing orphan bytes."""
        meta = self.metadata(gen)
        path = self.path(gen)
        if path.is_symlink():
            raise SegmentCorrupt("Symlink segment")
        entries: list[dict[str, Any]] = []
        seq = meta["base_seq"]
        previous = meta["base_digest"]
        end = meta["seal_seq"] if meta["phase"] == "SEALED" else None
        with path.open("rb") as stream:
            first = stream.readline(4097)
            if first != (canonical(self.header(meta)) + "\n").encode():
                raise SegmentCorrupt("Header mismatch")
            if end == seq:
                if meta["seal_digest"] != previous:
                    raise SegmentCorrupt("Sealed anchor mismatch")
                return entries
            total = len(first)
            while True:
                line = stream.readline(8193)
                if not line:
                    break
                total += len(line)
                if total > self.LIMIT or len(line) > 8192 or not line.endswith(b"\n"):
                    raise SegmentCorrupt("Partial or oversized segment tail")
                try:
                    receipt = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    raise SegmentCorrupt("Invalid segment JSON") from None
                try:
                    unsigned = {k: receipt[k] for k in ("envelope", "digest")}
                    if receipt["proof"] != self.ledger.mac("n1.receipt.v1", unsigned):
                        raise ValueError()
                    env = receipt["envelope"]
                    body = env["body"]
                    if env["mac"] != self.ledger.mac("n1.intent.v1", body):
                        raise ValueError()
                    if body["gen"] != gen or body["seq"] != seq + 1 or body["prev"] != previous:
                        raise ValueError()
                    if receipt["digest"] != sha(previous + "\n" + canonical(env)):
                        raise ValueError()
                except (KeyError, TypeError, ValueError):
                    raise SegmentCorrupt("Invalid physical receipt chain") from None
                entries.append(receipt)
                seq = body["seq"]
                previous = receipt["digest"]
                if end is not None and seq == end:
                    if previous != meta["seal_digest"]:
                        raise SegmentCorrupt("Sealed anchor mismatch")
                    return entries
        if end is not None and (seq != end or previous != meta["seal_digest"]):
            raise SegmentCorrupt("Missing sealed prefix")
        return entries

    def append(
        self,
        envelope: dict[str, Any],
        *,
        before_write: Callable[[], object] | None = None,
        after_fsync: Callable[[], object] | None = None,
    ) -> dict[str, Any]:
        """Real append+fsync outside DB locks. Callbacks are deterministic test barriers."""
        body = envelope["body"]
        gen = body["gen"]
        meta = self.metadata(gen)
        if meta["phase"] != "READY" or meta["state"] != "ACTIVE":
            raise Refused("SEGMENT_NOT_READY")
        receipt = self.ledger.simulated_writer_receipt(envelope)
        with self.repo.connect() as db:
            row = db.execute(
                "SELECT body,mac,state FROM n1_ledger WHERE intent=?", (body["intent_id"],)
            ).fetchone()
            if (
                not row
                or row["body"] != canonical(body)
                or row["mac"] != envelope["mac"]
                or row["state"] != "PENDING"
            ):
                raise Refused("UNKNOWN_APPEND_INTENT")
        path = self.path(gen)
        with projection_lock(path):
            entries = self.scan(gen)
            if entries and entries[-1]["envelope"]["body"]["intent_id"] == body["intent_id"]:
                if entries[-1] != receipt:
                    raise Refused("APPEND_IDENTITY_CONFLICT")
                return receipt
            tip_seq = entries[-1]["envelope"]["body"]["seq"] if entries else meta["base_seq"]
            tip_digest = entries[-1]["digest"] if entries else meta["base_digest"]
            if body["seq"] != tip_seq + 1 or body["prev"] != tip_digest:
                raise Refused("PHYSICAL_CAS_FAILED")
            if before_write:
                before_write()
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "ab") as stream:
                stream.write((canonical(receipt) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
            if after_fsync:
                after_fsync()
            return receipt

    def confirm(
        self, job: str, token: str, attempt: int, receipt: dict[str, Any]
    ) -> dict[str, Any]:
        # Authenticate physical presence before short DB adoption; a concurrent seal
        # still fences the ledger confirmation in its DB transaction.
        gen = receipt["envelope"]["body"]["gen"]
        entries = self.scan(gen)
        if receipt not in entries:
            raise Refused("RECEIPT_NOT_IN_CONFIRMED_PREFIX")
        return self.ledger.confirm(job, token, attempt, receipt)

    def accepted(self, job: str) -> list[Any]:
        receipts = self.ledger.view(job)
        cache: dict[str, list[dict[str, Any]]] = {}
        for receipt in receipts:
            gen = receipt["envelope"]["body"]["gen"]
            if gen not in cache:
                cache[gen] = self.scan(gen)
            if receipt not in cache[gen]:
                raise SegmentCorrupt("Ledger/physical receipt mismatch")
        return receipts

    def seal(self, gen: str, *, failpoint: Callable[[str], object] | None = None) -> str:
        """Logical freeze and child generation PREPARING are one DB-only commit."""
        with self.repo.transaction() as db:
            old = db.execute("SELECT * FROM n1_segments WHERE gen=?", (gen,)).fetchone()
            meta = db.execute("SELECT * FROM n1_segment_files WHERE gen=?", (gen,)).fetchone()
            if not old or not meta:
                raise Refused("UNKNOWN_SEGMENT")
            if old["state"] == "SEALED":
                child = db.execute(
                    "SELECT gen FROM n1_segment_files WHERE parent=?", (gen,)
                ).fetchone()
                if not child:
                    raise Refused("MISSING_GENERATION_CHILD")
                new: str = child["gen"]
            else:
                new = uuid.uuid4().hex
                db.execute("UPDATE n1_segments SET state='SEALED' WHERE gen=?", (gen,))
                db.execute(
                    "UPDATE n1_ledger SET state='UNKNOWN' WHERE gen=? AND state='PENDING'", (gen,)
                )
                db.execute(
                    "UPDATE n1_segment_files SET phase='SEALED',seal_seq=?,seal_digest=? WHERE gen=?",
                    (old["seq"], old["digest"], gen),
                )
                db.execute(
                    "INSERT INTO n1_segments VALUES(?,'ACTIVE',?,?)",
                    (new, old["seq"], old["digest"]),
                )
                db.execute(
                    "INSERT INTO n1_segment_files VALUES(?,'PREPARING',?,?,?,NULL,NULL)",
                    (new, gen, old["seq"], old["digest"]),
                )
        if failpoint:
            failpoint("after_logical_seal")
        self._prepare(new, failpoint)
        return new
