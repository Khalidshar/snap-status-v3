"""Read-only, cached NetworkClient-compatible checks and Google topic guards."""

import hashlib
import time
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from studio_core.network import NetworkResponse
from studio_core.source_checks import check
from studio_core.sources import for_command
from studio_core.sources.news import TITLES, google_matches


def feed(title, at):
    date = datetime.fromtimestamp(at, UTC).isoformat()
    raw = (
        f"<rss><channel><item><title>{title}</title>"
        f"<link>https://news.google.com/one</link><pubDate>{date}</pubDate>"
        "</item></channel></rss>"
    ).encode()
    return NetworkResponse(200, "text/xml", raw, hashlib.sha256(raw).hexdigest())


def test_every_command_has_arabic_google_fallback():
    for key in TITLES:
        assert any(
            s.id.startswith("gnews-") and s.language == "ar" and s.kind == "rss"
            for s in for_command(key)
        )


def test_check_distinguishes_forbidden_stopped_and_no_fresh_and_caches():
    now = time.time()

    def get(url):
        if "aawsat" in url:
            return NetworkResponse(403, "text/plain", b"", hashlib.sha256(b"").hexdigest())
        if "bbc" in url:
            return NetworkResponse(503, "text/plain", b"", hashlib.sha256(b"").hexdigest())
        return feed("عنوان عربي قديم - مصدر عربي", now - 90 * 86400)

    client = Mock()
    client.get.side_effect = get
    rows = check(client, now)
    assert {r["command"] for r in rows} == set(TITLES)
    assert {r["state"] for r in rows} >= {"403", "متوقف", "ما فيه خبر حديث"}
    assert client.get.call_count == len({r["url"] for r in rows})


def test_current_matching_source_is_working_without_exposing_headline():
    now = time.time()
    client = Mock()
    client.get.return_value = feed("تقنية جديدة للحواسيب - مصدر عربي", now - 10)
    rows = check(client, now)
    assert any(r["state"] == "يشتغل" and r["command"] == "tech" for r in rows)
    assert all("headline" not in row for row in rows)


@pytest.mark.parametrize(
    "key,title",
    [
        ("tech", "فوز فريق في مباراة كرة القدم"),
        ("football", "بطولة كرة السلة الجديدة"),
        ("style", "موضة جديدة في سيارات السباق"),
        ("markets", "طبق طعام جديد في مطعم"),
        ("urgent", "هجوم على أسعار الوجبات"),
        ("saudi", "حفل في فرنسا"),
        ("world", "افتتاح متجر محلي"),
    ],
)
def test_google_rejects_wrong_section(key, title):
    assert not google_matches(key, title)
