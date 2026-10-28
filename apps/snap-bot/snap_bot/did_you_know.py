"""Reviewed local facts and conservative, owned 30-day reservations. No network."""

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from studio_core.jobs import Repository

from .weather import content_hash

CATALOG = Path(__file__).with_name("data") / "did_you_know.ar.json"
WINDOW_SECONDS = 30 * 86400
FOOTER = "تابعني للمزيد 👻"
CATEGORIES = ("animals", "body", "geography", "technology", "space")
HOSTS = {
    "science.nasa.gov",
    "ocean.si.edu",
    "www.si.edu",
    "howthingsfly.si.edu",
    "www.genome.gov",
    "home.cern",
    "www.rfc-editor.org",
    "www.nist.gov",
    "pubmed.ncbi.nlm.nih.gov",
    "oceanservice.noaa.gov",
    "www.niams.nih.gov",
    "www.ncbi.nlm.nih.gov",
    "www.nei.nih.gov",
    "www.nidcd.nih.gov",
    "whc.unesco.org",
    "www.gps.gov",
    "www.qrcode.com",
    "nfc-forum.org",
    "www.eink.com",
    "www.bluetooth.com",
}


HISTORY_LIMIT = 1000
DEFAULT_KIND = "did_you_know"
# Each kind is one reviewed catalog with its own on-screen title. A loader returns facts
# shaped like the did_you_know catalog: id, category, text, source, source_url, verified_at.
KINDS: dict[str, tuple[str, Callable[[date], list[dict[str, Any]]]]] = {}
# Sacred kinds carry verbatim faith text: no proofreading, wrapping or display edits.
SACRED_KINDS: set[str] = set()


class FactsExhausted(ValueError):
    pass


def safe_text(value: object, limit: int) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= limit or value != value.strip():
        raise ValueError("Invalid fact text")
    if any(
        ord(c) < 32 or c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069" for c in value
    ):
        raise ValueError("Unsafe fact text")


def validate_fact(fact: Any, today: date) -> dict[str, Any]:
    if not isinstance(fact, dict) or set(fact) != {
        "id",
        "category",
        "text",
        "source",
        "source_url",
        "verified_at",
    }:
        raise ValueError("Incomplete curated fact")
    if not isinstance(fact["id"], str) or not re.fullmatch("dyk-[0-9]{2}", fact["id"]):
        raise ValueError("Invalid fact ID")
    if fact["category"] not in {"space", "animals", "body", "geography", "technology"}:
        raise ValueError("Invalid fact category")
    safe_text(fact["text"], 120)
    safe_text(fact["source"], 28)
    safe_text(fact["source_url"], 512)
    url = urlsplit(fact["source_url"])
    if (
        url.scheme != "https"
        or url.hostname not in HOSTS
        or url.username
        or url.password
        or url.port not in (None, 443)
    ):
        raise ValueError("Unapproved fact source")
    if not isinstance(fact["verified_at"], str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", fact["verified_at"]
    ):
        raise ValueError("Invalid verification date")
    if date.fromisoformat(fact["verified_at"]) > today:
        raise ValueError("Future verification date")
    return fact


def load_facts(path: str | Path = CATALOG, today: date | None = None) -> list[dict[str, Any]]:
    today = today or datetime.now(UTC).date()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != 1 or data.get("language") != "ar" or data.get("sacred") is not False:
        raise ValueError("Unapproved catalog")
    facts = data.get("facts")
    if not isinstance(facts, list) or not facts:
        raise ValueError("Empty catalog")
    ids: set[str] = set()
    texts: set[str] = set()
    for fact in facts:
        validate_fact(fact, today)
        if fact["id"] in ids or fact["text"] in texts:
            raise ValueError("Duplicate catalog fact")
        ids.add(fact["id"])
        texts.add(fact["text"])
    return facts


def register_kind(
    kind: str,
    title: str,
    loader: Callable[[date], list[dict[str, Any]]],
    *,
    sacred: bool = False,
) -> None:
    if not re.fullmatch("[a-z][a-z_]{1,23}", kind) or kind in KINDS:
        raise ValueError("Invalid or duplicate content kind")
    safe_text(title, 40)
    KINDS[kind] = (title, loader)
    if sacred:
        SACRED_KINDS.add(kind)


def load_kind(kind: str, today: date | None = None) -> list[dict[str, Any]]:
    """The facts of one reviewed kind. Every loader validates its whole catalog."""
    if kind not in KINDS:
        raise ValueError("Unknown content kind")
    today = today or datetime.now(UTC).date()
    facts = KINDS[kind][1](today)
    ids = {fact["id"] for fact in facts}
    if len(ids) != len(facts) or not facts:
        raise ValueError("Invalid content kind catalog")
    return facts


def categories_of(facts: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(fact["category"] for fact in facts))


def make_content(fact: dict[str, Any], now: float, kind: str = DEFAULT_KIND) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError("Unknown content kind")
    value: dict[str, Any] = {
        "type": "did_you_know",
        "sacred": kind in SACRED_KINDS,
        "language": "ar",
        "title": KINDS[kind][0],
        "body": fact["text"],
        "fact_id": fact["id"],
        "category": fact["category"],
        "source": "curated-local",
        "source_display": fact["source"],
        "source_url": fact["source_url"],
        "verified_at": fact["verified_at"],
        "attribution": "المصدر: " + fact["source"]
        if fact["source"] != "original"
        else "روّق مع خالد",
        "footer": FOOTER,
        "fetched_at": datetime.fromtimestamp(now, UTC).isoformat(),
        "expires_at": now + 3600,
    }
    # The original kind keeps its exact fields, so its existing content hashes stay valid.
    if kind != DEFAULT_KIND:
        value["kind"] = kind
    value["content_hash"] = content_hash(value)
    return value


def content_kind(value: dict[str, Any]) -> str:
    kind = value.get("kind", DEFAULT_KIND)
    if not isinstance(kind, str) or kind not in KINDS or (kind == DEFAULT_KIND and "kind" in value):
        raise ValueError("Unknown content kind")
    return kind


def is_sacred(value: dict[str, Any]) -> bool:
    """The content's sacred flag, which must match its kind exactly."""
    kind = content_kind(value)
    if value.get("sacred") is not (kind in SACRED_KINDS):
        raise ValueError("Unapproved fact content")
    return kind in SACRED_KINDS


def validate_content(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("type") != "did_you_know":
        raise ValueError("Unapproved fact content")
    is_sacred(value)
    now = datetime.now(UTC).timestamp()
    kind = content_kind(value)
    fact = next((f for f in load_kind(kind) if f["id"] == value.get("fact_id")), None)
    if fact is None:
        raise ValueError("Unknown curated fact")
    expected = make_content(fact, now, kind)
    # Expiry and fetch timestamp are deliberately excluded by the existing hash.
    if (
        content_hash(value) != expected["content_hash"]
        or value.get("content_hash") != expected["content_hash"]
    ):
        raise ValueError("Curated content changed")
    if type(value.get("expires_at")) not in (int, float) or not math.isfinite(value["expires_at"]):
        raise ValueError("Invalid content expiry")
    return fact


def reservation_state(
    db: sqlite3.Connection,
    now: float,
    *,
    exclude_job: str | None = None,
    kind: str = DEFAULT_KIND,
) -> tuple[set[tuple[str, str]], str | None]:
    """Protected (fact id, body hash) pairs and the last reserved category.

    Shared by choose_fact and the independent memory/rotation agents. Confirmed
    use is timed from ACK; active or unknown effects stay reserved.
    """
    history = [
        row
        for row in db.execute(
            "SELECT r.*,j.state,j.updated_at FROM fact_reservations r JOIN jobs j ON j.id=r.job_id ORDER BY r.rowid DESC"
        ).fetchall()
        if row["job_id"] != exclude_job
    ]
    has_outbox = (
        db.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_outbox'").fetchone()
        is not None
    )
    blocked: set[tuple[str, str]] = set()
    for row in history:
        evidence = db.execute(
            "SELECT state FROM deliveries WHERE job_id=?", (row["job_id"],)
        ).fetchall()
        ack_at = None
        if has_outbox:
            ack_at = db.execute(
                "SELECT MAX(o.updated_at) FROM telegram_outbox o JOIN deliveries d ON (d.id=o.video_sha OR d.id=o.video_sha||':'||d.job_id) WHERE d.job_id=? AND d.state='PUBLISHED' AND o.state='PUBLISHED'",
                (row["job_id"],),
            ).fetchone()[0]
        if ack_at is None and any(item["state"] == "PUBLISHED" for item in evidence):
            ack_at = row["updated_at"]
        # Terminal failures without ANY delivery evidence release immediately.
        protected = (
            ack_at > now - WINDOW_SECONDS
            if ack_at is not None
            else bool(evidence) or row["state"] not in {"FAILED", "CANCELLED", "EXPIRED"}
        )
        if protected:
            blocked.add(("id", row["fact_id"]))
            blocked.add(("body", row["body_hash"]))
    # Repeats are blocked across every kind; rotation follows the same kind only.
    mine = [
        row for row in history if json.loads(row["content_json"]).get("kind", DEFAULT_KIND) == kind
    ]
    last = json.loads(mine[0]["content_json"])["category"] if mine else None
    seen = (
        db.execute("SELECT 1 FROM sqlite_master WHERE name='fact_history'").fetchone() is not None
    )
    if seen:
        rows = db.execute("SELECT * FROM fact_history ORDER BY at DESC").fetchall()
        for row in rows:
            if row["until"] > now:
                blocked.add(("id", row["fact_id"]))
                blocked.add(("body", row["body_hash"]))
        own = [row for row in rows if (row["kind"] or DEFAULT_KIND) == kind]
        if own and (not mine or own[0]["at"] > mine[0]["reserved_at"]):
            last = own[0]["category"]
    return blocked, last


def history_entry(entry: Any) -> tuple[str, str, str, float, float, str]:
    """One fact the owner was already shown, as the server ledger recorded it."""
    keys = {"fact_id", "body_hash", "category", "at", "until"}
    if not isinstance(entry, dict) or not keys <= set(entry) <= keys | {"kind"}:
        raise ValueError("Invalid fact history")
    times = (entry["at"], entry["until"])
    kind = entry.get("kind", DEFAULT_KIND)
    if (
        not isinstance(entry["fact_id"], str)
        # Catalog IDs: dyk-07, tech-01, and the faith IDs bukhari_1 or muslim_2564.
        or not re.fullmatch("[a-z][a-z0-9_-]{1,39}", entry["fact_id"])
        or not isinstance(entry["body_hash"], str)
        or not re.fullmatch("[0-9a-f]{64}", entry["body_hash"])
        or not isinstance(entry["category"], str)
        or not re.fullmatch("[a-z][a-z_]{1,23}", entry["category"])
        or not isinstance(kind, str)
        or not re.fullmatch("[a-z][a-z_]{1,23}", kind)
        or any(type(t) not in (int, float) or not math.isfinite(t) for t in times)
        or entry["until"] < entry["at"]
        or kind not in KINDS
    ):
        raise ValueError("Invalid fact history")
    allowed = CATEGORIES if kind == DEFAULT_KIND else categories_of(load_kind(kind))
    if entry["category"] not in allowed:
        raise ValueError("Invalid fact history")
    return (
        entry["fact_id"],
        entry["body_hash"],
        entry["category"],
        entry["at"],
        entry["until"],
        kind,
    )


def seed_history(db: sqlite3.Connection, entries: Any) -> None:
    """Carry facts shown in earlier renders into a fresh job database.

    Each server render starts from an empty database, so without this every render
    would reserve the same first fact. Rows block their fact until `until` and the
    newest one sets the rotation category, through reservation_state.
    """
    if not isinstance(entries, list) or len(entries) > HISTORY_LIMIT:
        raise ValueError("Invalid fact history")
    rows = [history_entry(entry) for entry in entries]
    db.execute(
        "CREATE TABLE IF NOT EXISTS fact_history(fact_id TEXT NOT NULL,body_hash TEXT NOT NULL,category TEXT NOT NULL,at REAL NOT NULL,until REAL NOT NULL,kind TEXT)"
    )
    db.executemany("INSERT INTO fact_history VALUES(?,?,?,?,?,?)", rows)


def rotation_order(last: str | None, categories: tuple[str, ...] = CATEGORIES) -> tuple[str, ...]:
    start = (categories.index(last) + 1) % len(categories) if last in categories else 0
    return categories[start:] + categories[:start]


def choose_fact(
    repo: Repository,
    job: str,
    token: str,
    attempt: int,
    *,
    catalog: str | Path = CATALOG,
    kind: str = DEFAULT_KIND,
) -> tuple[dict[str, Any], list[Any]]:
    now = repo.clock()
    day = datetime.fromtimestamp(now, UTC).date()
    facts = load_facts(catalog, day) if kind == DEFAULT_KIND else load_kind(kind, day)
    with repo.owned_transaction(job, token, attempt=attempt) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS fact_reservations(job_id TEXT PRIMARY KEY REFERENCES jobs(id),fact_id TEXT NOT NULL,body_hash TEXT NOT NULL,reserved_at REAL NOT NULL,content_json TEXT NOT NULL)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS fact_reservation_window ON fact_reservations(reserved_at,fact_id,body_hash)"
        )
        prior = db.execute(
            "SELECT content_json FROM fact_reservations WHERE job_id=?", (job,)
        ).fetchone()
        if prior:
            return json.loads(prior["content_json"]), []
        blocked, last = reservation_state(db, now, kind=kind)
        rotation = rotation_order(
            last, CATEGORIES if kind == DEFAULT_KIND else categories_of(facts)
        )
        ordered = [fact for category in rotation for fact in facts if fact["category"] == category]
        for fact in ordered:
            digest = hashlib.sha256(fact["text"].encode()).hexdigest()
            if ("id", fact["id"]) in blocked or ("body", digest) in blocked:
                continue
            content = make_content(fact, now, kind)
            db.execute(
                "INSERT INTO fact_reservations(job_id,fact_id,body_hash,reserved_at,content_json) VALUES(?,?,?,?,?)",
                (
                    job,
                    fact["id"],
                    digest,
                    now,
                    json.dumps(content, ensure_ascii=False, sort_keys=True),
                ),
            )
            repo.event(
                db,
                job,
                "fact.reserved",
                {"fact_id": fact["id"], "attempt": attempt, "window_seconds": WINDOW_SECONDS},
            )
            return content, []
        raise FactsExhausted("No fact available inside the 30-day exclusion window")


register_kind(DEFAULT_KIND, "هل تعلم؟", lambda today: load_facts(CATALOG, today))
# The variety kinds register themselves; imported last, after everything above exists.
from . import sacred_kinds, variety_kinds  # noqa: E402,F401
