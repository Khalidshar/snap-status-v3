"""Approved daily hadith data, Umm al-Qura dates and an append-only per-bot cycle."""

import hashlib
import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from studio_core.religious.ummalqura import convert

CATALOG = Path(__file__).with_name("data") / "hadith-calendar.json"
SEASONS = {"general", "ramadan", "hajj", "ashura", "eid_fitr", "eid_adha"}


def load(path: Path | None = None) -> list[dict[str, Any]]:
    path = CATALOG if path is None else path
    try:
        with path.open("rb") as stream:
            raw = stream.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("HADITH_DAILY_CATALOG_INVALID")
        data = json.loads(raw)
    except (OSError, ValueError) as error:
        raise ValueError("HADITH_DAILY_CATALOG_UNAVAILABLE") from error
    if not isinstance(data, dict) or data.get("approved") is not True:
        raise ValueError("HADITH_DAILY_NOT_APPROVED")
    if (
        type(data.get("schema")) is not int
        or data["schema"] != 1
        or data.get("calendar") != "umm_al_qura"
        or not isinstance(data.get("entries"), list)
        or not 1 <= len(data["entries"]) <= 366
    ):
        raise ValueError("HADITH_DAILY_CATALOG_INVALID")
    seen_ids, seen_texts, seen_days = set(), set(), set()
    rows = []
    for item in data["entries"]:
        if not isinstance(item, dict):
            raise ValueError("HADITH_DAILY_ENTRY_INVALID")
        text, identifier = item.get("text"), item.get("id")
        month, day = item.get("hijri_month"), item.get("hijri_day")
        seasons = item.get("seasons")
        if not isinstance(item.get("narrator_url"), str):
            raise ValueError("HADITH_DAILY_ENTRY_INVALID")
        source = urlsplit(item.get("narrator_url", ""))
        if (
            not isinstance(identifier, str)
            or not re.fullmatch("[a-z0-9_-]{1,100}", identifier)
            or not isinstance(text, str)
            or not text.strip()
            or item.get("approved") is not True
            or item.get("grade") != "صحيح"
            or not isinstance(item.get("source"), str)
            or not item["source"].strip()
            or not isinstance(item.get("narrator"), str)
            or not item["narrator"].strip()
            or item.get("sha256") != hashlib.sha256(text.encode()).hexdigest()
            or not isinstance(item.get("source_sha256"), str)
            or not re.fullmatch("[0-9a-f]{64}", item["source_sha256"])
            or source.scheme != "https"
            or source.netloc not in {"sunnah.com", "dorar.net"}
            or not source.path.strip("/")
            or source.query
            or source.fragment
            or type(month) is not int
            or not 1 <= month <= 12
            or type(day) is not int
            or not 1 <= day <= 30
            or not isinstance(seasons, list)
            or not seasons
            or any(not isinstance(s, str) or s not in SEASONS for s in seasons)
        ):
            raise ValueError("HADITH_DAILY_ENTRY_INVALID")
        if identifier in seen_ids or item["sha256"] in seen_texts or (month, day) in seen_days:
            raise ValueError("HADITH_DAILY_DUPLICATE")
        seen_ids.add(identifier)
        seen_texts.add(item["sha256"])
        seen_days.add((month, day))
        rows.append(dict(item))
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


def choose(day: date, db_path: Path, app: str, job: str) -> dict[str, Any]:
    from .hadith_voice import read

    if app not in {"snap-bot-v2", "status-bot-v2"} or not re.fullmatch("[0-9a-f]{32}", job):
        raise ValueError("HADITH_DAILY_SCOPE_INVALID")
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
            read(selected["id"], selected["text"])
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
