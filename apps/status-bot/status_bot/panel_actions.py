"""WhatsApp panel executor (owner decision 7 Nov 2026): every queued panel action runs or closes.

produce/retry build one clip for review and send nothing. approve sends that exact saved clip
once, through the same upload/send path as a chat command (window, HALT, no replay).
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from studio_core.jobs import panel_workspace as workspace
from studio_core.jobs.component_lock import ComponentLocks

from . import content_clips
from .command_control import change
from .whatsapp_contract import require


def media(path: Path, preview: str, suffix: str) -> Path:
    return path.parent / "panel-media" / (preview + suffix)


def produce(outbox: Any, path: Path, identifier: str, command: str) -> bool:
    require(not content_clips.safe_path(content_clips.STATE / "HALT").exists(), "WA_SEND_HALTED")
    item = {"tag": hashlib.sha256(("panel:" + identifier).encode()).hexdigest()}
    content_clips.initialize(outbox.repo)
    locks = ComponentLocks(outbox.repo)
    lease = locks.acquire("command-clips", "USE", 1200)
    try:
        content, result, video = content_clips.build(outbox, item, command)
    finally:
        locks.release(lease)
    workspace.record_preview(path, identifier, command, video, [str(content.get("title", ""))[:60]])
    side = media(path, identifier, ".json")
    with side.open("x") as stream:
        json.dump(
            {"tag": item["tag"], "command": command, "content": content, "result": result}, stream
        )
    side.chmod(0o600)
    return True


class ApprovalBlockedBeforeSend(RuntimeError):
    """The same preview can be explicitly approved again."""


def approve(outbox: Any, transport: Any, runtime: Any, path: Path, preview: str, sha: str) -> bool:
    raw = media(path, preview, ".mp4").read_bytes()
    require(hashlib.sha256(raw).hexdigest() == sha, "PANEL_PREVIEW_CHANGED")
    saved = json.loads(media(path, preview, ".json").read_text())
    item, key = {"tag": saved["tag"]}, saved["command"]
    locks = ComponentLocks(outbox.repo)
    lease = locks.acquire("command-clips", "USE", 1200)
    try:
        content_clips.initialize(outbox.repo)
        with outbox.repo.transaction() as db:
            changed = db.execute(
                "INSERT INTO command_videos(tag,binding,kind,state,at) VALUES(?,?,?,'WORKING',?)"
                " ON CONFLICT(tag) DO UPDATE SET state='WORKING',at=excluded.at,"
                " code=NULL,video_sha=NULL WHERE command_videos.state='BLOCKED'"
                " AND command_videos.binding=excluded.binding AND command_videos.kind=excluded.kind",
                (item["tag"], outbox.identity.binding, key, outbox.repo.clock()),
            ).rowcount
            require(changed == 1, "WA_COMMAND_ALREADY_CONSUMED")
        try:
            content_clips.deliver_built(
                outbox, transport, item, key, saved["content"], saved["result"], raw, runtime,
                root=content_clips.ROOT, source=None, locks=locks, lease=lease,
            )  # fmt: skip
        except Exception as error:
            if content_clips.close_failure(outbox, item["tag"], error):
                raise ApprovalBlockedBeforeSend from error
            raise
    finally:
        locks.release(lease)
    return True


def run(outbox: Any, transport: Any, runtime: Any, path: Path, item: dict[str, Any]) -> bool:
    action, payload = item["action"], item["payload"]
    if action == "produce":
        return produce(outbox, path, item["id"], payload["command"])
    if action == "retry":
        with sqlite3.connect(path) as db:
            row = db.execute(
                "SELECT command FROM panel_previews WHERE id=?", (payload["preview"],)
            ).fetchone()
        return bool(row) and produce(outbox, path, item["id"], row[0])
    if action == "approve":
        return approve(outbox, transport, runtime, path, payload["preview"], payload["sha"])
    if action == "reject":
        return True  # the panel already marked it REJECTED; nothing was sent
    if action == "rate":
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS panel_ratings(preview TEXT PRIMARY KEY,"
                " rating INTEGER, at REAL)"
            )
            db.execute(
                "INSERT INTO panel_ratings VALUES(?,?,strftime('%s','now'))"
                " ON CONFLICT(preview) DO UPDATE SET rating=excluded.rating, at=excluded.at",
                (payload["preview"], int(payload["rating"])),
            )
        return True
    if action in {"source_add", "source_approve", "schedule"}:
        return True  # saved by the panel; no timer is ever started
    if action == "server":
        if payload["target"] != "wsl" or payload["operation"] not in {"stop", "start"}:
            return False
        change(payload["operation"] == "stop", runtime, "panel-" + item["id"])
        return True
    return False


def process(outbox: Any, transport: Any, runtime: Any, path: Path) -> bool:
    if not path.is_file():
        return False
    item = workspace.claim(path)
    if item is None:
        return False
    ok = retry_preview = False
    try:
        ok = run(outbox, transport, runtime, path, item)
    except ApprovalBlockedBeforeSend:
        retry_preview = True
    except Exception as error:  # closes as UNKNOWN; never retried or replayed
        print("STATUS_PANEL_ACTION_FAILED " + type(error).__name__, flush=True)
        ok = False
    finally:
        workspace.finish(path, item["id"], ok, retry_preview=retry_preview)
    return True
