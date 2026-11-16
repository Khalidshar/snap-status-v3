"""Read-only preflight for an owner-run upgrade; no bot imports, writes or secret reads."""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import tomllib
from contextlib import closing
from pathlib import Path
from typing import Any

PENDING = "مقطع ينتظر موافقتك؛ النشر يلغيه"


def state_root() -> Path:
    default = Path(__file__).resolve().parents[2] / "config/defaults.toml"
    config = Path(os.environ.get("STUDIO_CONFIG", str(default)))
    value = (
        tomllib.loads(config.read_text(encoding="utf-8"))
        .get("runtime", {})
        .get("state_root", "/var/lib/madar")
    )
    if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("UPGRADE_STATE_ROOT_INVALID")
    return Path(value)


def safe(path: Path) -> Path:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("UPGRADE_STATE_SYMLINK")
    return path


def read_json(path: Path) -> Any:
    return json.loads(safe(path).read_text(encoding="utf-8"))


def events(path: Path) -> list[dict[str, Any]]:
    if not safe(path).exists():
        return []
    raw = path.read_bytes()
    if len(raw) > 32_000_000 or (raw and not raw.endswith(b"\n")):
        raise ValueError("UPGRADE_LEDGER_INCOMPLETE")
    previous = "0" * 64
    result = []
    for seq, line in enumerate(raw.splitlines(), 1):
        event = json.loads(line)
        signature = event.pop("hash")
        canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if (
            event["seq"] != seq
            or event["previous"] != previous
            or hashlib.sha256(canonical.encode()).hexdigest() != signature
        ):
            raise ValueError("UPGRADE_LEDGER_CORRUPT")
        result.append(event)
        previous = signature
    return result


def snap_pending(root: Path, now: float) -> int:
    records = events(root / "snap/events.jsonl")
    pending = root / "snap/commands/pending.json"
    cancelled = read_json(pending).get("cancelled", []) if safe(pending).exists() else []
    if not isinstance(cancelled, list):
        raise ValueError("UPGRADE_PENDING_STATE_INVALID")
    terminal = {
        "INTENT",
        "ACK",
        "ACK_EVIDENCE_PENDING",
        "UNKNOWN",
        "PRE_NETWORK_BLOCKED",
        "CANCELLED",
        "EXPIRED",
    }
    closed = {e.get("video_sha") for e in records if e.get("kind") in terminal}
    count = 0
    for event in records:
        if (
            event.get("kind") != "RENDERED"
            or event.get("video_sha") in closed
            or event.get("video_sha") in cancelled
        ):
            continue
        preview = event.get("preview_id")
        if not isinstance(preview, str) or not re.fullmatch("[0-9a-f]{32}", preview):
            raise ValueError("UPGRADE_PREVIEW_INVALID")
        data = read_json(root / "snap/previews" / preview / "preview.json")
        expiry = data["expires_at"]
        if type(expiry) not in (int, float) or not math.isfinite(expiry):
            raise ValueError("UPGRADE_PREVIEW_EXPIRY_INVALID")
        if expiry > now:
            count += 1
    return count


def check(root: Path, *, force_pending: bool = False) -> dict[str, int]:
    now = time.time()
    live = pending = 0
    safe(root)
    files = sorted({*root.glob("*/commands/jobs.sqlite"), *root.glob("*/jobs.sqlite")})
    for path in files:
        safe(path)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only=ON")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "component_locks" in tables:
                live += db.execute(
                    "SELECT count(*) FROM component_locks WHERE deadline>?", (now,)
                ).fetchone()[0]
            if (
                path in {root / "status/commands/jobs.sqlite", root / "status/jobs.sqlite"}
                and "command_videos" in tables
            ):
                pending += db.execute(
                    "SELECT count(*) FROM command_videos WHERE state='BLOCKED'"
                    " AND code='TRANSLATION_REVIEW_REQUIRED'"
                ).fetchone()[0]
    if live:
        raise ValueError("UPGRADE_LIVE_LOCK: a build holds a live lock")
    pending += snap_pending(root, now)
    if pending and not force_pending:
        raise ValueError(PENDING)
    return {"live_locks": live, "pending_clips": pending}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-pending", action="store_true")
    args = parser.parse_args()
    try:
        counts = check(state_root(), force_pending=args.force_pending)
        for key, value in counts.items():
            print(f"{key}={value}")
    except ValueError as error:
        message = str(error)
        if message == PENDING or message.startswith("UPGRADE_"):
            print("STOP: " + message)
        else:
            print("STOP: UPGRADE_STATE_UNREADABLE")
        return 1
    except (OSError, sqlite3.Error, KeyError, TypeError, AttributeError):
        print("STOP: UPGRADE_STATE_UNREADABLE")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
