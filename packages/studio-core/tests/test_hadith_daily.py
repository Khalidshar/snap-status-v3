"""Unapproved data stays inert; dates, seasons and cycles are shared by both bots."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from studio_core.religious import hadith_daily, hadith_text_check, hadith_voice

DAY = date(2024, 3, 11)  # 1 Ramadan 1445 in Umm al-Qura.


def row(n, *, month=9, day=1, seasons=None):
    text = f"نَصٌّ اخْتِبَارِيٌّ غَيْرُ دِينِيٍّ {n}"
    return {
        "id": f"test_{n}",
        "text": text,
        "unmarked_positions": [6, 13, 29],
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source": "مصدر اختباري",
        "narrator": "اختبار",
        "narrator_url": "https://sunnah.com/bukhari:1",
        "grade": "صحيح",
        "approved": True,
        "hijri_month": month,
        "hijri_day": day,
        "seasons": seasons or ["general"],
    }


def install(tmp_path, monkeypatch, entries, approved=True):
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "approved": approved,
                "calendar": "umm_al_qura",
                "entries": entries,
            }
        )
    )
    sources = tmp_path / "source-snapshots"
    sources.mkdir(exist_ok=True)
    for item in entries:
        (sources / (item["id"] + ".txt")).write_bytes(item["text"].encode())
    monkeypatch.setattr(hadith_text_check, "SOURCES", sources)
    monkeypatch.setattr(hadith_daily, "CATALOG", path)
    monkeypatch.setattr(hadith_voice, "read", lambda *a: b"synthetic binding fixture")
    return tmp_path / "history.sqlite"


def test_umm_al_qura_and_out_of_range_have_no_estimated_fallback():
    assert hadith_daily.hijri(DAY) == (1445, 9, 1)
    with pytest.raises(ValueError, match="UMM_AL_QURA_DATE_UNAVAILABLE"):
        hadith_daily.hijri(date(1800, 1, 1))
    assert hadith_daily.seasons(12, 10) == {"general", "hajj", "eid_adha"}
    assert hadith_daily.seasons(10, 1) == {"general", "eid_fitr"}
    assert hadith_daily.seasons(1, 10) == {"general", "ashura"}


def test_pending_catalog_does_not_create_history_or_import_batch_paper(tmp_path, monkeypatch):
    db = install(tmp_path, monkeypatch, [], approved=False)
    with pytest.raises(ValueError, match="NOT_APPROVED"):
        hadith_daily.choose(DAY, db, "snap-bot-v2", "a" * 32)
    assert not db.exists()


@pytest.mark.parametrize("app", ["snap-bot-v2", "status-bot-v2"])
def test_no_repeat_before_complete_cycle_and_no_records_are_removed(app, tmp_path, monkeypatch):
    db = install(tmp_path, monkeypatch, [row(n, day=n + 1) for n in range(3)])
    chosen = [hadith_daily.choose(DAY, db, app, f"{n:032x}")["id"] for n in range(4)]
    assert len(set(chosen[:3])) == 3 and chosen[3] == chosen[0]
    assert hadith_daily.choose(DAY, db, app, "0" * 32)["id"] == chosen[0]
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT cycle FROM hadith_daily_choices ORDER BY rowid"
        ).fetchall() == [(1,), (1,), (1,), (2,)]
    other = "status-bot-v2" if app == "snap-bot-v2" else "snap-bot-v2"
    assert hadith_daily.choose(DAY, db, other, "0" * 32)["id"] == chosen[0]


def test_season_priority_and_exact_date_are_deterministic(tmp_path, monkeypatch):
    db = install(tmp_path, monkeypatch, [row(1), row(2, day=2, seasons=["ramadan"])])
    assert hadith_daily.choose(DAY, db, "snap-bot-v2", "a" * 32)["id"] == "test_2"
    assert hadith_daily.choose(DAY, db, "snap-bot-v2", "b" * 32)["id"] == "test_1"


def test_seasonal_exhaustion_does_not_reset_an_incomplete_cycle(tmp_path, monkeypatch):
    db = install(tmp_path, monkeypatch, [row(1), row(2, day=2, seasons=["ramadan"])])
    winter = date(2024, 1, 1)
    hadith_daily.choose(winter, db, "snap-bot-v2", "a" * 32)
    with pytest.raises(ValueError, match="SEASON_EXHAUSTED"):
        hadith_daily.choose(winter, db, "snap-bot-v2", "b" * 32)


def test_concurrent_choices_cannot_reserve_the_same_id(tmp_path, monkeypatch):
    db = install(tmp_path, monkeypatch, [row(n, day=n + 1) for n in range(3)])
    with ThreadPoolExecutor(max_workers=3) as pool:
        picked = list(
            pool.map(
                lambda n: hadith_daily.choose(DAY, db, "snap-bot-v2", f"{n:032x}")["id"], range(3)
            )
        )
    assert len(set(picked)) == 3


def test_missing_voice_does_not_consume_a_choice(tmp_path, monkeypatch):
    db = install(tmp_path, monkeypatch, [row(1)])

    def missing(*args):
        raise ValueError("HADITH_VOICE_UNAVAILABLE")

    monkeypatch.setattr(hadith_voice, "read", missing)
    with pytest.raises(ValueError, match="VOICE_UNAVAILABLE"):
        hadith_daily.choose(DAY, db, "snap-bot-v2", "a" * 32)
    with sqlite3.connect(db) as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='hadith_daily_choices'"
        ).fetchone()


def test_duplicate_or_unapproved_or_changed_text_is_rejected(tmp_path, monkeypatch):
    for entries in (
        [row(1), row(1, day=2)],
        [{**row(1), "approved": False}],
        [{**row(1), "text": "نص مختلف"}],
    ):
        install(tmp_path, monkeypatch, entries)
        with pytest.raises(ValueError):
            hadith_daily.load()
