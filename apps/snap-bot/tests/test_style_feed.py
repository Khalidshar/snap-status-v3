"""Synthetic XML fixtures only; no live requests or translation packages."""

import json
from pathlib import Path

import pytest

from snap_bot import style_feed as feed

FIXTURES = Path(__file__).with_name("fixtures") / "style"
SOURCE = {
    "url": "https://example.com/rss",
    "name": "مصدر تجريبي",
    "language": "ar",
    "section": "news",
}


def test_rss_filters_deduplicates_and_sorts() -> None:
    items = feed.parse_feed((FIXTURES / "rss.xml").read_bytes(), SOURCE)
    assert [i["link"] for i in items] == [
        "https://example.com/makeup",
        "https://example.com/perfume",
    ]
    assert items[1]["date"] == "2026-10-29T06:00:00+00:00"
    assert all(i["source"] == SOURCE["name"] for i in items)
    assert all(set(i) == {"title", "link", "date", "source"} for i in items)


def test_atom_alternate_links_dates_and_html_rejection() -> None:
    items = feed.parse_feed((FIXTURES / "atom.xml").read_bytes(), SOURCE)
    assert [i["link"] for i in items] == [
        "https://example.com/collection",
        "https://example.com/lipstick",
    ]


def test_section_filter_and_persistent_caller_deduplication() -> None:
    xml = (FIXTURES / "rss.xml").read_bytes()
    items = feed.parse_feed(xml, {**SOURCE, "section": "beauty"})
    assert any(i["link"].endswith("politics") for i in items)  # explicit dedicated source section
    seen: set[str] = set()
    assert feed.deduplicate(items, seen) == items
    assert feed.deduplicate(items, seen) == []
    assert feed.title_fingerprint("New Perfume") == feed.title_fingerprint(" new  perfume ")


@pytest.mark.parametrize(
    "title",
    [
        "<b>perfume</b>",
        "&lt;b&gt;perfume&lt;/b&gt;",
        "عطر\n",
        "عطر\t",
        "عطر\u202e",
        "عطر\x00",
        "&#x202e;عطر",
    ],
)
def test_unsafe_title_rejected(title: str) -> None:
    with pytest.raises(ValueError):
        feed.title_fingerprint(title)


@pytest.mark.parametrize(
    "xml",
    [
        b"<html/>",
        b"<rss>",
        b'<!DOCTYPE rss [<!ENTITY x "bad">]><rss/>',
        b"\xff",
        b"x" * (feed.MAX_BYTES + 1),
    ],
)
def test_bad_document_fails_closed(xml: bytes) -> None:
    with pytest.raises(ValueError):
        feed.parse_feed(xml, SOURCE)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://user:pass@example.com/rss",
        "https://example.com/%0a",
        "https://example.com/ a",
    ],
)
def test_invalid_source_url(url: str) -> None:
    with pytest.raises(ValueError):
        feed.parse_feed(b"<rss/>", {**SOURCE, "url": url})


def test_catalog_excludes_unverified_candidates() -> None:
    catalog = json.loads(feed.DATA.read_text())
    assert feed.sources() == []
    assert catalog["candidates"]
    for source in catalog["candidates"]:
        assert 0 < len(source["name"]) <= 20
        assert source["language"] in {"ar", "en"}
        assert source["permission_url"] and source["official_feed_page"]
        assert source["status"] == "pending_reachability_validation"


def test_nested_title_and_naive_date_rejected() -> None:
    xml = b"<rss><channel><item><title><b>perfume</b></title><link>https://example.com/x</link><pubDate>2026-10-29</pubDate></item></channel></rss>"
    assert feed.parse_feed(xml, SOURCE) == []
