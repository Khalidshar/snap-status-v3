"""Read-only, cached NetworkClient-compatible checks and publisher source guards."""

import hashlib
import time
from datetime import UTC, datetime
from unittest.mock import Mock
from urllib.parse import urlsplit

from studio_core.network import NetworkResponse
from studio_core.source_checks import check
from studio_core.sources.news import TITLES, news_sources


def feed(url, title, at):
    date = datetime.fromtimestamp(at, UTC).isoformat()
    raw = (
        f"<rss><channel><item><title>{title}</title>"
        f"<link>https://{urlsplit(url).hostname}/one</link><pubDate>{date}</pubDate>"
        "</item></channel></rss>"
    ).encode()
    return NetworkResponse(200, "text/xml", raw, hashlib.sha256(raw).hexdigest())


def test_every_command_has_a_direct_arabic_publisher_feed():
    for key in (*TITLES, "news_noon", "brief"):
        feeds = news_sources(key)
        assert feeds, key
        assert all(urlsplit(s.url).hostname != "news.google.com" for s in feeds)


def test_named_command_routes_to_the_right_publisher_sections():
    markets = {s.id for s in news_sources("markets", production=True)}
    football = {s.id for s in news_sources("football", production=True)}
    noon = {s.id for s in news_sources("news_noon", production=True)}
    assert "aawsat-economy" in markets - football
    assert "aawsat-sport" in football - markets
    assert "aawsat" in noon and "aawsat-economy" not in noon


def test_check_distinguishes_forbidden_stopped_and_no_fresh_and_caches():
    now = time.time()

    def get(url):
        if "aawsat" in url:
            return NetworkResponse(403, "text/plain", b"", hashlib.sha256(b"").hexdigest())
        if "bbc" in url:
            return NetworkResponse(503, "text/plain", b"", hashlib.sha256(b"").hexdigest())
        return feed(url, "عنوان عربي قديم", now - 90 * 86400)

    client = Mock()
    client.get.side_effect = get
    rows = check(client, now)
    assert {r["command"] for r in rows} == set(TITLES)
    assert {r["state"] for r in rows} >= {"403", "متوقف", "ما فيه خبر حديث"}
    assert client.get.call_count == len({r["url"] for r in rows})


def test_current_matching_source_is_working_without_exposing_headline():
    now = time.time()
    client = Mock()
    client.get.side_effect = lambda url: feed(url, "تقنية جديدة للحواسيب", now - 10)
    rows = check(client, now)
    assert any(r["state"] == "يشتغل" and r["command"] == "tech" for r in rows)
    assert all("headline" not in row for row in rows)
