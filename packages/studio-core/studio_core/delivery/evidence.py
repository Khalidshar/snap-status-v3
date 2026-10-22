"""Shared delivery evidence: durable receipts and network-free reconciliation.

Graduated from the lab core 0.11.0 (snap-bot-v2 #52, status-bot-v2 #13) with
types added; the behaviour is unchanged. Sending itself stays in each
application: it holds the owner checks and the never-resend outbox. Nothing here
opens a network connection or retries a delivery; it only repairs local
evidence from a durable PUBLISHED acknowledgement.
"""

import json
import os
import sqlite3
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from studio_core.config import Config
from studio_core.jobs.repository import Repository
from studio_core.storage.workspace import Storage

Reconcile = Callable[[Config, str, str], dict[str, Any]]


class DeliveryConfirmed(RuntimeError):
    """The external ACK is durable; local evidence may need repair. Never resend."""

    def __init__(self, job_id: str, video_sha: str, review_mode: str | None = None) -> None:
        super().__init__("Delivery confirmed; local evidence requires reconciliation")
        self.job_id = job_id
        self.video_sha = video_sha
        self.review_mode = review_mode


def atomic_json(destination: Path, value: Any) -> None:
    fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".receipt-")
    try:
        with os.fdopen(fd, "w", encoding="utf8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, destination)
        # The rename itself must survive a crash before the receipt counts.
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def receipt_metadata(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS telegram_receipt_metadata("
        "job_id TEXT PRIMARY KEY,video_sha TEXT NOT NULL,review_mode TEXT NOT NULL)"
    )


def remember_review(
    db: sqlite3.Connection, job_id: str, video_sha: str, review_mode: str | None
) -> None:
    receipt_metadata(db)
    # First approved mode is durable and cannot be replaced by a later default.
    db.execute(
        "INSERT OR IGNORE INTO telegram_receipt_metadata VALUES(?,?,?)",
        (job_id, video_sha, review_mode or "NOT_RECORDED"),
    )


def multipart(chat: int | str, video: bytes, caption: str, filename: str) -> tuple[bytes, str]:
    boundary = "studio" + uuid.uuid4().hex
    chunks = []
    for name, value in {
        "chat_id": str(chat),
        "caption": caption,
        "supports_streaming": "true",
    }.items():
        chunks.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            ).encode()
        )
    chunks.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; "
            f'name="video"; filename="{filename}"\r\nContent-Type: video/mp4\r\n\r\n'
        ).encode()
        + video
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def reconcile_published(
    config: Config,
    job_id: str,
    video_sha: str,
    review_mode: str | None = None,
    *,
    bot: str,
    manifest_state: str | None = None,
) -> dict[str, Any]:
    """Read only the durable ACK; repair local evidence, without network or a lease."""
    repo = Repository(config)
    with repo.transaction() as db:
        receipt_metadata(db)
        metadata = db.execute(
            "SELECT review_mode FROM telegram_receipt_metadata WHERE job_id=? AND video_sha=?",
            (job_id, video_sha),
        ).fetchone()
        row = db.execute(
            "SELECT * FROM telegram_outbox WHERE video_sha=? AND state='PUBLISHED'", (video_sha,)
        ).fetchone()
        linked = db.execute(
            "SELECT 1 FROM deliveries WHERE job_id=? AND state='PUBLISHED' AND id IN (?,?) LIMIT 1",
            (job_id, video_sha, video_sha + ":" + job_id),
        ).fetchone()
        if not row or not linked or not isinstance(row["message_id"], int):
            raise ValueError("Confirmed delivery receipt missing")
        repo.reconcile_delivery(job_id, db=db)
        job = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["state"] != "COMPLETED":
            raise ValueError("Published job requires state reconciliation")
    root = Storage(config).workspace(job_id)
    manifest: dict[str, Any] = (
        json.loads((root / "manifest.json").read_text())
        if (root / "manifest.json").exists()
        else {"job_id": job_id}
    )
    prior = manifest.get("delivery_receipt", {})
    mode = (
        metadata["review_mode"]
        if metadata
        else prior.get("review_mode") or review_mode or "NOT_RECORDED"
    )
    proof = {
        "delivery": "PUBLISHED",
        "bot": bot,
        "message_id": row["message_id"],
        "video_sha256": video_sha,
        "job_id": job_id,
        "review_mode": mode,
    }
    # Delivery evidence does not invent a visual review PASS.
    atomic_json(root / "output/delivery-result.json", proof)
    # Status also records the terminal state in its manifest; Snap keeps its existing shape.
    if manifest_state is not None:
        manifest.update(state=manifest_state)
    manifest.update(external_delivery=True, delivery="PUBLISHED", delivery_receipt=proof)
    Storage(config).manifest(job_id, manifest)
    slice_path = root / "output/slice-result.json"
    if slice_path.exists():
        value = json.loads(slice_path.read_text())
        value.update(state="COMPLETED", external_delivery=True, delivery_receipt=proof)
        atomic_json(slice_path, value)
    return proof


def reconcile_evidence_sweep(
    config: Config, after: str = "", limit: int = 100, *, bot: str, reconcile: Reconcile
) -> dict[str, Any]:
    """Bounded rotating scan of durable ACKs; no network and no delivery retries."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("Invalid sweep limit")
    repo = Repository(config)
    with repo.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_outbox'").fetchone():
            return {"after": "", "repaired": 0, "pending": 0, "skipped": 0}
        rows = db.execute(
            """SELECT DISTINCT j.id,o.video_sha FROM jobs j
          JOIN deliveries d ON d.job_id=j.id AND d.state='PUBLISHED'
          JOIN telegram_outbox o ON o.state='PUBLISHED'
            AND d.id IN (o.video_sha,o.video_sha||':'||j.id)
          WHERE j.state IN ('COMPLETED','DELIVERING','FAILED')
            AND j.id>? ORDER BY j.id LIMIT ?""",
            (after, limit),
        ).fetchall()
    # Page by stable job ID. Failures are revisited on the next rotation.
    repaired = pending = skipped = 0
    for row in rows:
        root = config.data_root / "renders" / row["id"]
        # GC is intentional: never recreate a collected workspace.
        if not root.exists():
            skipped += 1
            continue
        if not _aligned(config, repo, root, row["id"], row["video_sha"], bot):
            try:
                reconcile(config, row["id"], row["video_sha"])
                repaired += 1
            except Exception:
                pending += 1
    return {
        "after": rows[-1]["id"] if len(rows) == limit else "",
        "repaired": repaired,
        "pending": pending,
        "skipped": skipped,
    }


def _aligned(
    config: Config, repo: Repository, root: Path, job_id: str, video_sha: str, bot: str
) -> bool:
    """True only when every local copy of the evidence matches the durable ACK."""

    def read(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf8"))

    try:
        manifest = read(root / "manifest.json")
        proof = read(root / "output/delivery-result.json")
        retained = read(config.data_root / "manifests" / (job_id + ".json"))
        slice_path = root / "output/slice-result.json"
        slice_value = read(slice_path) if slice_path.exists() else None
        with repo.connect() as db:
            published = db.execute(
                "SELECT message_id FROM telegram_outbox WHERE video_sha=?", (video_sha,)
            ).fetchone()
            current = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()["state"]
        expected = {
            "delivery": "PUBLISHED",
            "bot": bot,
            "job_id": job_id,
            "video_sha256": video_sha,
            "message_id": published["message_id"],
        }

        def good(value: dict[str, Any]) -> bool:
            return all(value.get(key) == wanted for key, wanted in expected.items())

        aligned = (
            current == "COMPLETED"
            and good(proof)
            and all(
                value.get("external_delivery") is True
                and value.get("delivery") == "PUBLISHED"
                and good(value.get("delivery_receipt", {}))
                for value in (manifest, retained)
            )
        )
        if slice_value is not None:
            aligned = (
                aligned
                and slice_value.get("state") == "COMPLETED"
                and slice_value.get("external_delivery") is True
                and good(slice_value.get("delivery_receipt", {}))
            )
        return bool(aligned)
    except Exception:
        return False
