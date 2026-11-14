"""Review stale panel/video intents locally; explicit apply, no deletion or replay."""

import argparse
import fcntl
import json
import math
import os
import re
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import switch_host as host

MIN_AGE = 3600


class ReviewRefused(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__("STALE_REVIEW_ARGUMENT_INVALID")
        self.reason = reason


REFUSAL_REASONS = {
    "APP_INVALID": "التطبيق غير صالح",
    "CUTOFF_INVALID": "الحد الزمني غير صالح",
    "CUTOFF_TOO_RECENT": "الحد أحدث من ساعة",
    "REASON_INVALID": "رمز سبب الإغلاق غير صالح",
    "SWITCH_CONSUMER_STILL_RUNNING": "خدمة ما زالت تعمل",
    "SWITCH_UNSUPERVISED_CONSUMER": "كاتب خارج إدارة الخدمات ما زال يعمل",
    "SWITCH_STATE_HANDLE_OPEN": "كاتب يحتفظ بملف حالة مفتوح",
    "STALE_REVIEW_COMPONENT_ACTIVE": "قفل مكوّن حي",
    "STALE_REVIEW_ROW_CHANGED": "تغير الصف أثناء المراجعة",
    "SWITCH_PATH_UNSAFE": "مسار غير آمن",
    "SWITCH_INVENTORY_CHANGED": "تغير جرد الخدمات",
    "SWITCH_UNIT_STATE_UNKNOWN": "تعذر التحقق من حالة خدمة",
    "SWITCH_UNIT_NAME_UNSAFE": "اسم خدمة غير آمن",
    "SWITCH_AUTOSTART_NOT_INERT": "خدمة بدء التشغيل ما زالت تعمل",
    "SWITCH_AUTOSTART_STATE_UNKNOWN": "تعذر التحقق من خدمة بدء التشغيل",
    "LOCK_BUSY": "قفل مشغول",
    "DATABASE_LOCKED": "قاعدة البيانات مقفلة",
    "DATABASE_ERROR": "تعذرت قراءة قاعدة البيانات أو تحديثها",
    "PERMISSION_DENIED": "صلاحية الملف أو القفل غير كافية",
    "FILE_MISSING": "ملف مطلوب غير موجود",
    "SYSTEM_CHECK_FAILED": "تعذر فحص الخدمات أو الكتّاب",
    "OS_ERROR": "تعذر الوصول إلى ملف أو قفل",
    "VALIDATION_FAILED": "فشل تحقق غير مصنف",
}


def refusal_reason(error: Exception) -> str:
    # Exact, fixed vocabulary only: never echo paths, process output or SQL values.
    if isinstance(error, ReviewRefused) and error.reason in REFUSAL_REASONS:
        return error.reason
    if isinstance(error, ValueError) and str(error) in REFUSAL_REASONS:
        return str(error)
    if isinstance(error, BlockingIOError):
        return "LOCK_BUSY"
    if isinstance(error, sqlite3.Error):
        if getattr(error, "sqlite_errorcode", None) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return "DATABASE_LOCKED"
        return "DATABASE_ERROR"
    if isinstance(error, PermissionError):
        return "PERMISSION_DENIED"
    if isinstance(error, FileNotFoundError):
        return "FILE_MISSING"
    if isinstance(error, subprocess.SubprocessError):
        return "SYSTEM_CHECK_FAILED"
    return "OS_ERROR" if isinstance(error, OSError) else "VALIDATION_FAILED"


def require_quiet(root: Path) -> None:
    names, _, _ = host.units()
    host.stopped(list(dict.fromkeys([*names, *host.LEGACY, *host.workers()])), root)
    for name in host.LIVE_LOCKS:
        path = root / name
        host.path_safe(path)
        if path.is_file():
            with path.open("rb") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def review(
    root: Path, app: str, before: float, reason: str, *, apply: bool = False
) -> list[dict[str, Any]]:
    now = time.time()
    if app not in {"snap", "status", "both"}:
        raise ReviewRefused("APP_INVALID")
    if not math.isfinite(before) or before < 0:
        raise ReviewRefused("CUTOFF_INVALID")
    if before > now - MIN_AGE:
        raise ReviewRefused("CUTOFF_TOO_RECENT")
    if not re.fullmatch("[A-Z][A-Z0-9_]{2,63}", reason):
        raise ReviewRefused("REASON_INVALID")
    host.path_safe(root)
    results = []
    for bot in ("snap", "status") if app == "both" else (app,):
        for relative in (f"{bot}/panel-commands.sqlite", f"{bot}/commands/jobs.sqlite"):
            path = root / relative
            host.path_safe(path)
            if not path.is_file():
                continue
            if apply:
                require_quiet(root)
            with (
                closing(
                    sqlite3.connect(
                        path.as_uri() + ("?mode=rw" if apply else "?mode=ro"), uri=True, timeout=5
                    )
                ) as db,
                db,
            ):
                db.row_factory = sqlite3.Row
                if apply:
                    db.execute("BEGIN IMMEDIATE")
                    require_quiet(root)
                tables = {
                    r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if (
                    apply
                    and "component_locks" in tables
                    and db.execute(
                        "SELECT 1 FROM component_locks WHERE deadline>? LIMIT 1", (now,)
                    ).fetchone()
                ):
                    raise ValueError("STALE_REVIEW_COMPONENT_ACTIVE")
                candidates = []
                if "panel_actions" in tables:
                    candidates.extend(
                        ("panel_actions", dict(r))
                        for r in db.execute(
                            "SELECT id,state,at FROM panel_actions "
                            "WHERE state IN ('INTENT','UNKNOWN') AND at<?",
                            (before,),
                        )
                    )
                if "command_videos" in tables:
                    candidates.extend(
                        ("command_videos", dict(r))
                        for r in db.execute(
                            "SELECT tag AS id,state,at,code FROM command_videos "
                            "WHERE state IN ('WORKING','UPLOAD_INTENT','SEND_INTENT') AND at<?",
                            (before,),
                        )
                    )
                for table, row in candidates:
                    terminal = "BLOCKED" if table == "panel_actions" else "BLOCKED_OR_UNKNOWN"
                    if apply:
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS switch_stale_closures("
                            "seq INTEGER PRIMARY KEY, table_name TEXT NOT NULL,"
                            "record_id TEXT NOT NULL, before_json TEXT NOT NULL,"
                            "new_state TEXT NOT NULL, reason TEXT NOT NULL,"
                            "cutoff_unix REAL NOT NULL, closed_at REAL NOT NULL)"
                        )
                        if table == "panel_actions":
                            changed = db.execute(
                                "UPDATE panel_actions SET state='BLOCKED' "
                                "WHERE id=? AND state=? AND at<?",
                                (row["id"], row["state"], before),
                            ).rowcount
                            if "panel_audit" in tables:
                                db.execute(
                                    "INSERT INTO panel_audit(at,action_id,event) VALUES(?,?,?)",
                                    (now, row["id"], "STALE_INTENT_BLOCKED:" + reason),
                                )
                        else:
                            changed = db.execute(
                                "UPDATE command_videos SET state='BLOCKED_OR_UNKNOWN',code=? "
                                "WHERE tag=? AND state=? AND at<?",
                                (reason, row["id"], row["state"], before),
                            ).rowcount
                        if changed != 1:
                            raise ValueError("STALE_REVIEW_ROW_CHANGED")
                        db.execute(
                            "INSERT INTO switch_stale_closures("
                            "table_name,record_id,before_json,new_state,reason,cutoff_unix,closed_at)"
                            " VALUES(?,?,?,?,?,?,?)",
                            (table, row["id"], json.dumps(row), terminal, reason, before, now),
                        )
                    results.append(
                        {
                            "app": bot,
                            "database": relative,
                            "table": table,
                            "id": row["id"],
                            "old_state": row["state"],
                            "new_state": terminal,
                            "applied": apply,
                            "reason": reason,
                        }
                    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=host.STATE)
    parser.add_argument("--app", choices=("snap", "status", "both"), default="both")
    parser.add_argument(
        "--before-unix",
        type=float,
        required=True,
        help="Reviewed fixed cutoff; must be at least one hour in the past",
    )
    parser.add_argument("--reason", required=True, help="Fixed uppercase audit code, no free text")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and os.geteuid() != 0:
        parser.error("التطبيق يحتاج root وموافقة خالد وإيقاف الخدمات أولًا")
    try:
        if args.apply:
            with Path("/run/lock/madar-switch.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                rows = review(args.state, args.app, args.before_unix, args.reason, apply=True)
        else:
            rows = review(args.state, args.app, args.before_unix, args.reason)
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        reason = refusal_reason(error)
        parser.exit(1, f"STALE_REVIEW_REFUSED reason={reason}; {REFUSAL_REASONS[reason]}\n")
    print(
        json.dumps(
            {"applied": args.apply, "cutoff_unix": args.before_unix, "rows": rows},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
