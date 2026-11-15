import pytest

from studio_core.production.prepare_base import safe_xml
from studio_core.sources.feeds import parse_feed

FEED = (
    b"\n<?xml version=\"1.0\" encoding=\"UTF-8\"?><rss version=\"2.0\"><channel>"
    b"<title>t</title><item><title>\xd8\xae\xd8\xa8\xd8\xb1 \xd8\xac\xd8\xaf\xd9\x8a\xd8\xaf</title>"
    b"<link>https://www.harpersbazaararabia.com/a</link>"
    b"<pubDate>Sun, 15 Nov 2026 08:00:00 GMT</pubDate></item></channel></rss>"
)


def test_safe_xml_accepts_blank_line_before_declaration():
    assert safe_xml(FEED).tag == "rss"


def test_safe_xml_malformed_is_value_error():
    with pytest.raises(ValueError):
        safe_xml(b"<rss><channel>")


def test_parse_feed_accepts_blank_line_before_declaration():
    items = parse_feed(
        FEED,
        {"name": "Harper's Bazaar Arabia", "url": "https://www.harpersbazaararabia.com/",
         "language": "ar", "section": "news"},
        style_only=False,
    )
    assert len(items) == 1
