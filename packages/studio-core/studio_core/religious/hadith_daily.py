"""Approved daily hadith data, Umm al-Qura dates and an append-only per-bot cycle."""

import copy
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from studio_core.religious import hadith_catalog
from studio_core.religious.ummalqura import convert

CATALOG = Path(__file__).with_name("data") / "hadith-calendar.json"
SEASONS = {"general", "ramadan", "hajj", "ashura", "eid_fitr", "eid_adha"}


def load(path: Path | None = None) -> list[dict[str, Any]]:
    rows = hadith_catalog.load(CATALOG if path is None else path)
    seen_days = set()
    for item in rows:
        month, day, seasons = item.get("hijri_month"), item.get("hijri_day"), item.get("seasons")
        if (
            type(month) is not int
            or not 1 <= month <= 12
            or type(day) is not int
            or not 1 <= day <= 30
            or not isinstance(seasons, list)
            or not seasons
            or any(not isinstance(s, str) or s not in SEASONS for s in seasons)
        ):
            raise ValueError("HADITH_DAILY_ENTRY_INVALID")
        if (month, day) in seen_days:
            raise ValueError("HADITH_DAILY_DUPLICATE")
        seen_days.add((month, day))
    return rows


def hijri(day: date) -> tuple[int, int, int]:
    return convert(day)


def seasons(month: int, day: int) -> set[str]:
    found = {"general"}
    if month == 9:
        found.add("ramadan")
    if month == 12 and day <= 13:
        found.add("hajj")
    if month == 1 and 9 <= day <= 11:
        found.add("ashura")
    if month == 10 and day <= 3:
        found.add("eid_fitr")
    if month == 12 and 10 <= day <= 13:
        found.add("eid_adha")
    return found


def choose(
    day: date,
    db_path: Path,
    app: str,
    job: str,
    *,
    validate_recording: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    if not callable(validate_recording):
        raise ValueError("HADITH_DAILY_RECORDING_VALIDATOR_REQUIRED")

    if app not in {"snap-bot-v2", "status-bot-v2"} or not re.fullmatch("[0-9a-f]{32}", job):
        raise ValueError("HADITH_DAILY_SCOPE_INVALID")
    if any(p.is_symlink() for p in (db_path, *db_path.absolute().parents)):
        raise ValueError("HADITH_DAILY_STATE_UNSAFE")
    rows = load()
    by_id = {item["id"]: item for item in rows}
    signature = hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    year, month, day_number = hijri(day)
    active = seasons(month, day_number)
    with sqlite3.connect(db_path, timeout=30) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS hadith_daily_choices("
            "app TEXT,job TEXT,id TEXT,cycle INTEGER,gregorian_date TEXT,hijri_date TEXT,"
            "catalog_sha256 TEXT,PRIMARY KEY(app,job),UNIQUE(app,cycle,id))"
        )
        existing = db.execute(
            "SELECT id,catalog_sha256,gregorian_date FROM hadith_daily_choices"
            " WHERE app=? AND job=?",
            (app, job),
        ).fetchone()
        if existing:
            if (
                existing[1] != signature
                or existing[0] not in by_id
                or existing[2] != day.isoformat()
            ):
                raise ValueError("HADITH_DAILY_CACHED_SCOPE_CHANGED")
            selected = by_id[existing[0]]
            if validate_recording(copy.deepcopy(selected)) is not True:
                raise ValueError("HADITH_DAILY_RECORDING_UNVERIFIED")
        else:
            cycle = db.execute(
                "SELECT COALESCE(MAX(cycle),1) FROM hadith_daily_choices WHERE app=?", (app,)
            ).fetchone()[0]
            used = {
                r[0]
                for r in db.execute(
                    "SELECT id FROM hadith_daily_choices WHERE app=? AND cycle=?", (app, cycle)
                )
            }
            if set(by_id) <= used:
                cycle += 1
                used = set()
            eligible = [
                item for item in rows if item["id"] not in used and set(item["seasons"]) & active
            ]
            if not eligible:
                raise ValueError("HADITH_DAILY_SEASON_EXHAUSTED")
            position = (month - 1) * 30 + day_number
            selected = min(
                eligible,
                key=lambda item: (
                    0 if (set(item["seasons"]) & (active - {"general"})) else 1,
                    ((item["hijri_month"] - 1) * 30 + item["hijri_day"] - position) % 360,
                    item["id"],
                ),
            )
            # A missing recording never consumes a daily choice or triggers synthesis.
            if validate_recording(copy.deepcopy(selected)) is not True:
                raise ValueError("HADITH_DAILY_RECORDING_UNVERIFIED")
            db.execute(
                "INSERT INTO hadith_daily_choices VALUES(?,?,?,?,?,?,?)",
                (
                    app,
                    job,
                    selected["id"],
                    cycle,
                    day.isoformat(),
                    f"{year:04d}-{month:02d}-{day_number:02d}",
                    signature,
                ),
            )
    return dict(selected)
