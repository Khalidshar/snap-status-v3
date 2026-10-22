"""Reviewed local facts and conservative, owned 30-day reservations. No network."""

import hashlib
import json
import math
import re
from datetime import UTC, date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

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


class FactsExhausted(ValueError):
    pass


def safe_text(value, limit):
    if not isinstance(value, str) or not 1 <= len(value) <= limit or value != value.strip():
        raise ValueError("Invalid fact text")
    if any(
        ord(c) < 32 or c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069" for c in value
    ):
        raise ValueError("Unsafe fact text")


def validate_fact(fact, today):
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


def load_facts(path=CATALOG, today=None):
    today = today or datetime.now(UTC).date()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != 1 or data.get("language") != "ar" or data.get("sacred") is not False:
        raise ValueError("Unapproved catalog")
    facts = data.get("facts")
    if not isinstance(facts, list) or not facts:
        raise ValueError("Empty catalog")
    ids = set()
    texts = set()
    for fact in facts:
        validate_fact(fact, today)
        if fact["id"] in ids or fact["text"] in texts:
            raise ValueError("Duplicate catalog fact")
        ids.add(fact["id"])
        texts.add(fact["text"])
    return facts


def make_content(fact, now):
    value = {
        "type": "did_you_know",
        "sacred": False,
        "language": "ar",
        "title": "هل تعلم؟",
        "body": fact["text"],
        "fact_id": fact["id"],
        "category": fact["category"],
        "source": "curated-local",
        "source_display": fact["source"],
        "source_url": fact["source_url"],
        "verified_at": fact["verified_at"],
        "attribution": "المصدر: " + fact["source"],
        "footer": FOOTER,
        "fetched_at": datetime.fromtimestamp(now, UTC).isoformat(),
        "expires_at": now + 3600,
    }
    value["content_hash"] = content_hash(value)
    return value


def validate_content(value):
    if value.get("type") != "did_you_know" or value.get("sacred") is not False:
        raise ValueError("Unapproved fact content")
    now = datetime.now(UTC).timestamp()
    fact = next((f for f in load_facts() if f["id"] == value.get("fact_id")), None)
    if fact is None:
        raise ValueError("Unknown curated fact")
    expected = make_content(fact, now)
    # Expiry and fetch timestamp are deliberately excluded by the existing hash.
    if (
        content_hash(value) != expected["content_hash"]
        or value.get("content_hash") != expected["content_hash"]
    ):
        raise ValueError("Curated content changed")
    if type(value.get("expires_at")) not in (int, float) or not math.isfinite(value["expires_at"]):
        raise ValueError("Invalid content expiry")
    return fact


def reservation_state(db, now, *, exclude_job=None):
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
    blocked = set()
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
    last = json.loads(history[0]["content_json"])["category"] if history else None
    return blocked, last


def rotation_order(last):
    start = (CATEGORIES.index(last) + 1) % len(CATEGORIES) if last in CATEGORIES else 0
    return CATEGORIES[start:] + CATEGORIES[:start]


def choose_fact(repo, job, token, attempt, *, catalog=CATALOG):
    now = repo.clock()
    facts = load_facts(catalog, datetime.fromtimestamp(now, UTC).date())
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
        blocked, last = reservation_state(db, now)
        rotation = rotation_order(last)
        ordered = [fact for category in rotation for fact in facts if fact["category"] == category]
        for fact in ordered:
            digest = hashlib.sha256(fact["text"].encode()).hexdigest()
            if ("id", fact["id"]) in blocked or ("body", digest) in blocked:
                continue
            content = make_content(fact, now)
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
