"""P0-B reference artifact/projection contracts. Not used by live services.
The filesystem root must be owned by a trusted publisher; UID enforcement is P1.
"""

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from studio_core.jobs import Repository, canonical

StrPath = str | os.PathLike[str]


class EvidenceConflict(RuntimeError):
    pass


class Unconfirmed(RuntimeError):
    pass


MAX_VIDEO = 50_000_000
RANK = {"AWAITING": 0, "UNKNOWN": 1, "PUBLISHED": 2}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verified_bytes(path: StrPath, expected: str) -> bytes:
    """Consume the SAME verified byte buffer; do not reopen source for upload."""
    if not re.fullmatch("[0-9a-f]{64}", expected):
        raise ValueError("Invalid SHA")
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Symlink source")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_VIDEO:
            raise ValueError("Invalid video size/type")
        data = source.read(MAX_VIDEO + 1)
        after = os.fstat(source.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("Source changed while consuming")
    if digest(data) != expected:
        raise ValueError("Video hash mismatch")
    return data


def published_path(root: StrPath, job: str, attempt: int, sha: str) -> Path:
    if (
        not re.fullmatch("[0-9a-f]{32}", job)
        or type(attempt) is not int
        or attempt < 1
        or not re.fullmatch("[0-9a-f]{64}", sha)
    ):
        raise ValueError("Invalid publication identity")
    root = Path(root)
    # Trusted root and parents are a deployment prerequisite, not proven by this helper.
    destination = root / "renders" / job / "attempts" / str(attempt) / "published" / (sha + ".mp4")
    cursor = root
    if cursor.is_symlink():
        raise ValueError("Symlink root")
    for part in destination.relative_to(root).parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Symlink publication directory")
        cursor.mkdir(exist_ok=True)
    return destination


def sync_directory(path: StrPath) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_snapshot(root: StrPath, job: str, attempt: int, data: bytes, expected: str) -> Path:
    """New inode, create-once publication. Not an OS account-isolation guarantee."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_VIDEO or digest(data) != expected:
        raise ValueError("Unverified publication bytes")
    target = published_path(root, job, attempt, expected)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=".snapshot-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), 0o400)
            os.fsync(stream.fileno())
        try:
            os.link(name, target)  # no-overwrite publication; same filesystem
        except FileExistsError:
            try:
                matches = not target.is_symlink() and verified_bytes(target, expected) == data
            except ValueError:
                matches = False
            if not matches:
                raise EvidenceConflict("Published bytes conflict")
        sync_directory(target.parent)
        return target
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def projection_lock(destination: StrPath) -> Iterator[None]:
    # Cross-process serialization in the isolated Linux publisher fixture.
    import fcntl

    path = Path(str(destination) + ".lock")
    if path.is_symlink():
        raise ValueError("Symlink projection lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class Projections:
    """SQLite projection authority; files are rebuildable, monotonically ranked views."""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        with repo.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS n1_projections(job_id TEXT PRIMARY KEY,state TEXT NOT NULL,revision INTEGER NOT NULL,body TEXT NOT NULL)"
            )

    def _confirmed(self, db: sqlite3.Connection, job: str, body: dict[str, Any]) -> None:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "telegram_outbox" not in tables:
            raise Unconfirmed("Missing durable ACK")
        sha = body.get("video_sha256")
        message = body.get("message_id")
        if (
            not isinstance(sha, str)
            or not re.fullmatch("[0-9a-f]{64}", sha)
            or type(message) is not int
            or message <= 0
        ):
            raise Unconfirmed("Invalid ACK identity")
        row = db.execute(
            "SELECT 1 FROM telegram_outbox WHERE video_sha=? AND state='PUBLISHED' AND message_id=?",
            (sha, message),
        ).fetchone()
        link = db.execute(
            "SELECT 1 FROM deliveries WHERE job_id=? AND state='PUBLISHED' AND id IN (?,?)",
            (job, sha, sha + ":" + job),
        ).fetchone()
        if not row or not link:
            raise Unconfirmed("Unverified durable ACK")

    def advance(
        self,
        job: str,
        state: str,
        body: dict[str, Any],
        *,
        token: str | None = None,
        attempt: int | None = None,
    ) -> Any:
        if state not in RANK or body.get("job_id") != job or body.get("state") != state:
            raise ValueError("Invalid projection")
        if set(body) - {"job_id", "state", "video_sha256", "message_id", "review_mode"}:
            raise ValueError("Unexpected projection field")
        encoded = canonical(body)
        with self.repo.transaction() as db:
            if state == "PUBLISHED":
                self._confirmed(db, job, body)
            else:
                # Lab behaviour kept: a missing token reaches the owner check and is refused there.
                self.repo.assert_owner(job, token, attempt=attempt, db=db)  # type: ignore[arg-type]
            old = db.execute("SELECT * FROM n1_projections WHERE job_id=?", (job,)).fetchone()
            if old:
                if RANK[old["state"]] > RANK[state]:
                    return json.loads(old["body"])
                if old["state"] == state and state == "PUBLISHED" and old["body"] != encoded:
                    raise EvidenceConflict("Confirmed receipt identity conflict")
                if old["body"] == encoded:
                    return json.loads(old["body"])
            revision = old["revision"] + 1 if old else 1
            db.execute(
                "INSERT INTO n1_projections VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET state=excluded.state,revision=excluded.revision,body=excluded.body",
                (job, state, revision, encoded),
            )
            return body

    def read(self, job: str) -> Any:
        with self.repo.connect() as db:
            row = db.execute("SELECT body FROM n1_projections WHERE job_id=?", (job,)).fetchone()
            return json.loads(row["body"]) if row else None

    def materialize(
        self, job: str, destination: StrPath, *, before_replace: Callable[[], object] | None = None
    ) -> Any:
        """No DB lock during filesystem work. Failure leaves DB truth for retry."""
        with projection_lock(destination):
            body = self.read(job)
            if body is None:
                raise ValueError("Projection missing")
            destination = Path(destination)
            if destination.is_symlink():
                raise ValueError("Symlink projection")
            fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".projection-")
            try:
                with os.fdopen(fd, "w", encoding="utf8") as stream:
                    stream.write(canonical(body))
                    stream.flush()
                    os.fsync(stream.fileno())
                if before_replace:
                    before_replace()  # test barrier, no production transport
                os.replace(name, destination)
                sync_directory(destination.parent)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            return body
