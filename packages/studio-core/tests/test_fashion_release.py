import sqlite3

from studio_core.observability.failure_code import NO_FRESH_FASHION_MESSAGE
from studio_core.production.reservations import release_content, release_job, story_urls


def ledger(tmp_path):
    db = tmp_path / "history.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE used(kind TEXT, fingerprint TEXT, job TEXT, at REAL,"
            " PRIMARY KEY(kind,fingerprint))"
        )
        conn.executemany(
            "INSERT INTO used VALUES(?,?,?,1)",
            [
                ("style_women", "https://a.example/1", "j1"),
                ("style", "https://a.example/1", "j1"),
                ("style_women", "https://a.example/2", "j2"),
                ("news", "https://a.example/1", "j1"),
            ],
        )
    return db


def rows(db):
    with sqlite3.connect(db) as conn:
        return sorted(conn.execute("SELECT kind,fingerprint,job FROM used").fetchall())


def content(urls, key="style_women"):
    return {"legacy_production": {"key": key, "scenes": [{"source_url": u} for u in urls]}}


def test_failed_build_frees_only_its_fashion_reservations(tmp_path):
    db = ledger(tmp_path)
    assert release_job(db, "j1") == 2
    assert rows(db) == [
        ("news", "https://a.example/1", "j1"),
        ("style_women", "https://a.example/2", "j2"),
    ]


def test_unsent_clip_gives_its_stories_back(tmp_path):
    db = ledger(tmp_path)
    assert release_content(db, content(["https://a.example/1"])) == 2
    assert ("style_women", "https://a.example/2", "j2") in rows(db)
    assert ("news", "https://a.example/1", "j1") in rows(db)


def test_non_fashion_or_missing_content_releases_nothing(tmp_path):
    db = ledger(tmp_path)
    assert story_urls(content(["https://a.example/1"], key="news")) == []
    assert release_content(db, None) == 0
    assert release_content(tmp_path / "missing.sqlite", content(["x"])) == 0
    assert len(rows(db)) == 4


def test_fashion_no_news_message_is_plain():
    assert NO_FRESH_FASHION_MESSAGE == "لا يوجد خبر أناقة جديد الآن"
