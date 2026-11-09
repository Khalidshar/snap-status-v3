"""List or close WhatsApp command videos left WORKING before PR #107."""

import argparse
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

DATABASE = Path("/var/lib/madar/status/commands/jobs.sqlite")
# Merge 0b34316f2b712c9b596990dfff23d6862e2ce37b, 2026-11-08 13:00:57 UTC.
FIX_MERGED_AT = 1794142857
REASON = "WA_STALE_WORKING_PRE_107"


def inspect(database: Path, *, apply: bool = False) -> list[dict[str, str | float]]:
    uri = database.resolve(strict=True).as_uri() + ("?mode=rw" if apply else "?mode=ro")
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db, db:
        db.row_factory = sqlite3.Row
        if apply:
            db.execute("BEGIN IMMEDIATE")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "command_videos" not in tables:
            return []
        rows = db.execute(
            "SELECT tag,kind,at FROM command_videos WHERE state='WORKING'"
            " AND at<? AND acked_at IS NULL AND message_tag IS NULL ORDER BY at,tag",
            (FIX_MERGED_AT,),
        ).fetchall()
        if apply and rows:
            if (
                "component_locks" in tables
                and db.execute(
                    "SELECT 1 FROM component_locks WHERE app='status-bot-v2'"
                    " AND component='command-clips' AND deadline>?",
                    (time.time(),),
                ).fetchone()
            ):
                raise ValueError("WA_COMMAND_CLIPS_ACTIVE")
            db.execute(
                "UPDATE command_videos SET state='BLOCKED_OR_UNKNOWN',code=? WHERE state='WORKING'"
                " AND at<? AND acked_at IS NULL AND message_tag IS NULL",
                (REASON, FIX_MERGED_AT),
            )
        state = "BLOCKED_OR_UNKNOWN" if apply else "WORKING"
        return [dict(row, state=state) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DATABASE)
    parser.add_argument("--apply", action="store_true", help="Close the listed rows")
    args = parser.parse_args()
    try:
        rows = inspect(args.database, apply=args.apply)
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(
        json.dumps(
            {
                "applied": args.apply,
                "cutoff_unix": FIX_MERGED_AT,
                "reason": REASON,
                "rows": rows,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
