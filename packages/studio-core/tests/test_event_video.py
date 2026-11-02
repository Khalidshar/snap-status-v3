import hashlib
from unittest.mock import Mock

import pytest

from studio_core.network.client import NetworkResponse
from studio_core.sources.event_video import prepare, verify
from studio_core.sources.feeds import parse_feed


def test_same_entry_enclosure_and_no_signed_query():
    xml = (
        b"<rss><channel><item><title>event</title>"
        b"<link>https://source.example/1</link>"
        b"<pubDate>2026-11-01T07:00:00Z</pubDate>"
        b'<enclosure type="video/mp4" url="https://cdn.example/1.mp4"/>'
        b"</item></channel></rss>"
    )
    source = {
        "name": "source",
        "url": "https://source.example/rss",
        "language": "ar",
        "section": "news",
    }
    assert parse_feed(xml, source, style_only=False)[0]["video_url"] == "https://cdn.example/1.mp4"
    assert (
        "video_url"
        not in parse_feed(xml.replace(b"1.mp4", b"1.mp4?token=hidden"), source, style_only=False)[0]
    )


def test_policy_and_exact_event_binding(tmp_path):
    content = {
        "source_id": "source",
        "source_video_url": "https://cdn.example/1.mp4",
        "content_hash": "a" * 64,
        "fingerprint": "b" * 64,
        "source_url": "https://source.example/1",
    }
    raw = b"\0\0\0\x18ftypisom"
    client = Mock()
    client.get.return_value = NetworkResponse(
        200, "video/mp4", raw, hashlib.sha256(raw).hexdigest()
    )
    assert prepare(content, tmp_path, client, {}) is None
    client.get.assert_not_called()
    manifest = prepare(content, tmp_path, client, {"source": ["cdn.example"]})
    assert verify(content, manifest).read_bytes() == raw
    with pytest.raises(ValueError, match="SOURCE_EVENT_MISMATCH"):
        verify({**content, "fingerprint": "c" * 64}, manifest)
    verify(content, manifest).chmod(0o600)
    verify(content, manifest).write_bytes(b"changed")
    with pytest.raises(ValueError, match="SOURCE_VIDEO_CHANGED"):
        verify(content, manifest)
