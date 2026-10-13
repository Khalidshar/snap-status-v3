"""Private SQLite snapshots and isolated restore probes; never resume jobs.

Snapshot DBs contain private application state. Do not commit or publish them.
Integrity hashes detect corruption, not malicious replacement by the owner.
"""

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path

from studio_core.observability.agent_inventory import APPLICATIONS


def _path(value):
    path = Path(value).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Symlink backup paths are forbidden")
    return path


def _private_directory(value):
    path = _path(value)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or path.stat().st_mode & 0o077:
        raise ValueError("Private backup directory required")
    return path


def _readonly(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)


def _check(db, app):
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("Snapshot integrity failed")
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("Snapshot reference integrity failed")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"jobs", "commands", "events", "deliveries"} <= tables:
        raise ValueError("Application database schema required")
    if db.execute("PRAGMA user_version").fetchone()[0] != 1:
        raise ValueError("Unsupported database schema version")
    if db.execute("SELECT 1 FROM jobs WHERE app IS NULL OR app != ? LIMIT 1", (app,)).fetchone():
        raise ValueError("Cross-application snapshot forbidden")
    return {
        # Table names come from the constant tuple below, never from input.
        table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]  # noqa: S608
        for table in ("jobs", "commands", "events", "deliveries")
    }


def _sync(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_snapshot(database, backup_root, app, revision, *, now=None, timeout_seconds=30):
    """Capture committed WAL state without constructing a mutable Repository."""
    if app not in APPLICATIONS:
        raise ValueError("Unknown application")
    if not isinstance(revision, str) or not re.fullmatch("[0-9a-f]{40}", revision):
        raise ValueError("Invalid source revision")
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
        raise ValueError("Invalid snapshot time")
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 300
    ):
        raise ValueError("Invalid snapshot timeout")
    deadline = time.monotonic() + timeout_seconds

    def progress(*args):
        if time.monotonic() >= deadline:
            raise TimeoutError("Snapshot time budget exceeded")

    source = _path(database)
    if not source.is_file():
        raise ValueError("Existing application database required")
    parent = _private_directory(backup_root)
    root = parent / (app + "-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    output = root / "jobs.sqlite"
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with closing(_readonly(source)) as original, closing(sqlite3.connect(output)) as target:
            original.backup(target, pages=128, progress=progress, sleep=0.01)
            target.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            counts = _check(target, app)
        _sync(output)
        manifest = {
            "schema": 1,
            "app": app,
            "revision": revision,
            "created_at": now,
            "file": "jobs.sqlite",
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "counts": counts,
            "scope": "DATABASE_ONLY",
            "restoration": "ISOLATED_PROBE_ONLY",
        }
        path = root / "manifest.json"
        with path.open("x", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            json.dump(manifest, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        _sync(root)
        return root
    except BaseException:
        shutil.rmtree(root)
        raise


def probe_restore(snapshot, app, scratch_root):
    """Verify a disposable restored copy; never overwrite or replay live state."""
    if app not in APPLICATIONS:
        raise ValueError("Unknown application")
    root = _path(snapshot)
    if not root.is_dir() or root.stat().st_mode & 0o077:
        raise ValueError("Private snapshot directory required")
    manifest_path, database = _path(root / "manifest.json"), _path(root / "jobs.sqlite")
    if any(p.stat().st_mode & 0o077 for p in (manifest_path, database)):
        raise ValueError("Private snapshot files required")
    if manifest_path.stat().st_size > 4096:
        raise ValueError("Invalid snapshot manifest")
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = {
        "schema",
        "app",
        "revision",
        "created_at",
        "file",
        "sha256",
        "counts",
        "scope",
        "restoration",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or value["app"] != app
        or value["file"] != "jobs.sqlite"
        or value["scope"] != "DATABASE_ONLY"
        or value["restoration"] != "ISOLATED_PROBE_ONLY"
        or not isinstance(value["revision"], str)
        or not re.fullmatch("[0-9a-f]{40}", value["revision"])
        or type(value["created_at"]) not in (int, float)
        or not math.isfinite(value["created_at"])
        or value["created_at"] <= 0
        or not isinstance(value["sha256"], str)
        or not re.fullmatch("[0-9a-f]{64}", value["sha256"])
    ):
        raise ValueError("Invalid snapshot binding")
    if (
        not isinstance(value["counts"], dict)
        or set(value["counts"]) != {"jobs", "commands", "events", "deliveries"}
        or any(type(count) is not int or count < 0 for count in value["counts"].values())
    ):
        raise ValueError("Invalid snapshot counts")
    # Standalone snapshots must not depend on untracked SQLite sidecars.
    if any(
        (root / ("jobs.sqlite" + suffix)).exists() or (root / ("jobs.sqlite" + suffix)).is_symlink()
        for suffix in ("-wal", "-shm", "-journal")
    ):
        raise ValueError("Unexpected snapshot sidecar")
    parent = _private_directory(scratch_root)
    with tempfile.TemporaryDirectory(prefix="restore-probe-", dir=parent) as temporary:
        target = Path(temporary) / "probe.sqlite"
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream, database.open("rb") as original:
            shutil.copyfileobj(original, stream)
        # Check the bytes of the restored copy, not only the original path.
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != value["sha256"]:
            raise ValueError("Snapshot digest mismatch")
        with closing(_readonly(target)) as restored:
            counts = _check(restored, app)
        if counts != value["counts"]:
            raise ValueError("Snapshot counts mismatch")
    return {
        "schema": 1,
        "app": app,
        "revision": value["revision"],
        "result": "PASS",
        "sha256": digest,
        "counts": counts,
        "scope": "DATABASE_ONLY",
        "live_state_modified": False,
        "jobs_resumed": False,
        "delivery_authority": False,
    }


def main(argv=None):
    import argparse

    from studio_core.config import Config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "probe"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--scratch-root", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config)
        if args.action == "snapshot":
            if args.backup_root is None or args.revision is None:
                raise ValueError("Snapshot arguments required")
            root = create_snapshot(
                config.data_root / "jobs.sqlite",
                args.backup_root,
                config.app,
                args.revision,
                timeout_seconds=args.timeout_seconds,
            )
            result = {
                "app": config.app,
                "snapshot": str(root),
                "scope": "DATABASE_ONLY",
                "jobs_resumed": False,
                "delivery_authority": False,
            }
        else:
            if args.snapshot is None or args.scratch_root is None:
                raise ValueError("Probe arguments required")
            result = probe_restore(args.snapshot, config.app, args.scratch_root)
    except (ValueError, OSError, sqlite3.Error, KeyError):
        print(json.dumps({"result": "FAIL", "code": "BACKUP_OR_PROBE_REJECTED"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
