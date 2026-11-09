"""One scheduled slot: produce and send the slot's clip in both bots (owner decision 9 Nov 2026).

Khalid: «تصنع وترسل»، ٣ مقاطع يوميًا للبوتين. The clip goes through the same panel actions as a
manual «أنتج الحين» then «اعتماد», so every check, frame and send rule still applies. A HALT
in either bot skips that bot. Times are Riyadh: morning 06:30, noon 13:00, night 21:30.
"""

import argparse
import json
import sqlite3
import sys
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages/studio-core"))
from studio_core.jobs import panel_workspace

STATE = Path("/var/lib/madar")
RIYADH = ZoneInfo("Asia/Riyadh")
NOON = ("facts", "tech", "markets", "style", "riddle", "funny", "history", "news")
WAIT = 45 * 60


def command_for(slot: str, day: date) -> str:
    if slot == "morning":
        return "kahf" if day.weekday() == 4 else "adhkar_am"  # Friday: سورة الكهف
    if slot == "noon":
        return NOON[day.toordinal() % len(NOON)]
    if slot == "night":
        return "isha" if day.toordinal() % 2 == 0 else "adhkar_pm"
    raise ValueError("UNKNOWN_SLOT")


def halted(app: str) -> bool:
    return any((STATE / app / name).exists() for name in ("HALT", "commands/HALT"))


def state_of(db: Path, action: str) -> str:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT state FROM panel_actions WHERE id=?", (action,)).fetchone()
    return row[0] if row else "NONE"


def wait(db: Path, action: str, deadline: float) -> str:
    while time.time() < deadline:
        current = state_of(db, action)
        if current not in {"QUEUED", "INTENT", "RUNNING", "NONE"}:
            return current
        time.sleep(15)
    return "TIMEOUT"


def preview_after(db: Path, command: str, since: float) -> tuple[str, str] | None:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT id, sha FROM panel_previews WHERE command=? AND state='REVIEW' AND at>=? "
            "ORDER BY at DESC LIMIT 1",
            (command, since - 5),
        ).fetchone()
    return (row[0], row[1]) if row else None


def run(app: str, command: str) -> dict[str, str]:
    db = STATE / app / "panel-commands.sqlite"
    if halted(app):
        return {"app": app, "command": command, "result": "SKIPPED_HALT"}
    started = time.time()
    produce = uuid.uuid4().hex
    panel_workspace.submit(db, produce, "produce", {"command": command})
    made = wait(db, produce, started + WAIT)
    found = preview_after(db, command, started) if made == "DONE" else None
    if found is None:
        return {"app": app, "command": command, "result": "PRODUCE_" + made}
    approve = uuid.uuid4().hex
    panel_workspace.submit(db, approve, "approve", {"preview": found[0], "sha": found[1]})
    sent = wait(db, approve, time.time() + WAIT)
    return {"app": app, "command": command, "result": "SEND_" + sent}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slot", choices=("morning", "noon", "night"))
    args = parser.parse_args()
    today = datetime.now(RIYADH).date()
    command = command_for(args.slot, today)
    for app in ("snap", "status"):
        try:
            outcome = run(app, command)
        except (OSError, ValueError, sqlite3.Error) as error:
            outcome = {"app": app, "command": command, "result": "ERROR_" + type(error).__name__}
        print("MADAR_SCHEDULE " + json.dumps(outcome | {"slot": args.slot}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
