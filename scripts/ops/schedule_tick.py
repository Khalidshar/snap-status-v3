"""One schedule tick, every ten minutes (owner 21 Nov 2026: «احتاج الجدوله نفس البوت السابق»).

Each bot's due clips (studio_core.jobs.schedule, the old bots' tables) go through the same
panel actions as a manual «أنتج الحين» then «اعتماد», so every check, agent and send rule
still applies and the clip reaches Khalid, who publishes it. Snap also runs the breaking-news
radar (at most six a day). HALT or the bot's own «إيقاف الجدولة» skips that bot. Progress is
kept per bot in <state>/<app>/schedule-done.json; a slot is attempted once.
"""

import argparse
import json
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages/studio-core"))
from studio_core.jobs import panel_workspace, schedule

STATE = Path("/var/lib/madar")
WAIT = 45 * 60
APPS = ("snap", "status")


def halted(app: str) -> bool:
    return any((STATE / app / name).exists() for name in ("HALT", "commands/HALT"))


def state_of(db: Path, action: str) -> str:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT state FROM panel_actions WHERE id=?", (action,)).fetchone()
    return str(row[0]) if row else "NONE"


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
    return (str(row[0]), str(row[1])) if row else None


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


def load(app: str) -> dict[str, Any]:
    path = STATE / app / "schedule-done.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    value.setdefault("done", [])
    value.setdefault("radar", {})
    return value


def save(app: str, value: dict[str, Any]) -> None:
    value["done"] = value["done"][-300:]
    value["radar"] = dict(sorted(value["radar"].items())[-7:])
    path = STATE / app / "schedule-done.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def report(outcome: dict[str, str], slot: str) -> None:
    print("MADAR_SCHEDULE " + json.dumps(outcome | {"slot": slot}), flush=True)


def safe_run(app: str, command: str) -> dict[str, str]:
    try:
        return run(app, command)
    except (OSError, ValueError, sqlite3.Error) as error:
        return {"app": app, "command": command, "result": "ERROR_" + type(error).__name__}


def tick(now: datetime | None = None) -> None:
    now = now or datetime.now(schedule.RIYADH)
    for app in APPS:
        if halted(app) or schedule.paused(STATE / app):
            continue
        progress = load(app)
        for key, command in schedule.due(app, now, set(progress["done"])):
            progress["done"].append(key)  # once per slot, even if it fails
            save(app, progress)
            report(safe_run(app, command), key)
        if app == "snap":
            day = now.astimezone(schedule.RIYADH).strftime("%Y%m%d")
            sent = int(progress["radar"].get(day, 0))
            if schedule.radar_open(now, sent):
                outcome = safe_run(app, schedule.RADAR)
                if outcome["result"] == "SEND_DONE":
                    progress["radar"][day] = sent + 1
                    save(app, progress)
                if outcome["result"] != "PRODUCE_FAILED":  # no fresh story: stay quiet
                    report(outcome, "radar")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("tick", "show"))
    parser.add_argument("--app", choices=APPS, default="snap")
    args = parser.parse_args()
    if args.action == "show":
        print(schedule.schedule_text(args.app, datetime.now(schedule.RIYADH), STATE / args.app))
    else:
        tick()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
