import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from studio_core.config import Config

if TYPE_CHECKING:
    from studio_core.jobs.repository import Repository


class Storage:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.root = config.data_root / "renders"
        self.metadata = config.data_root / "manifests"

    def workspace(self, job_id: str) -> Path:
        if not re.fullmatch("[0-9a-f]{32}", job_id):
            raise ValueError("Invalid job path")
        root = self.root / job_id
        if root.resolve().parent != self.root.resolve() or root.is_symlink():
            raise ValueError("Workspace escapes root")
        for part in ("input", "media", "audio", "design", "temp", "output"):
            path = root / part
            if path.is_symlink():
                raise ValueError("Unsafe asset directory")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root

    def manifest(self, job_id: str, value: Any) -> None:
        root = self.workspace(job_id)
        self.metadata.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.metadata.is_symlink():
            raise ValueError("Unsafe metadata directory")
        # Retained independently of bulky assets. Unique exclusive temp files avoid symlink writes.
        for destination in (root / "manifest.json", self.metadata / (job_id + ".json")):
            fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".manifest-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(value, stream, ensure_ascii=False, indent=2)
                os.replace(name, destination)
            finally:
                if os.path.exists(name):
                    os.unlink(name)

    def gc(self, repository: Repository) -> list[str]:
        cutoff = repository.clock() - self.config.retention_seconds
        deleted = []
        with repository.transaction() as db:
            rows = db.execute(
                "SELECT id FROM jobs WHERE artifacts_terminal_at<? AND state IN "
                "('COMPLETED','FAILED','CANCELLED','EXPIRED')",
                (cutoff,),
            ).fetchall()
            for row in rows:
                pending = db.execute(
                    "SELECT COUNT(*) FROM deliveries WHERE job_id=? AND state NOT IN "
                    "('PUBLISHED','MANUAL_CONFIRMED','EXPIRED','FAILED_ARCHIVED')",
                    (row["id"],),
                ).fetchone()[0]
                if pending:
                    continue
                root = self.root / row["id"]
                if root.exists():
                    if root.is_symlink() or root.resolve().parent != self.root.resolve():
                        raise ValueError("Unsafe GC path")
                    shutil.rmtree(root)
                    deleted.append(row["id"])
                    repository.event(db, row["id"], "storage.collected", {})
        return deleted
