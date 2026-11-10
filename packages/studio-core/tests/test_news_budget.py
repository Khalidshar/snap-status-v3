"""Snap news keeps a margin over the one-minute Spotlight revenue floor (owner 10 Nov 2026)."""

from studio_core.production.model import NEWS, budget


def test_snap_news_at_least_65_seconds():
    for key in NEWS:
        assert budget("snap-bot-v2", key)[0] == 65.0


def test_other_snap_and_whatsapp_unchanged():
    assert budget("snap-bot-v2", "facts")[0] == 61.0
    assert budget("snap-bot-v2", "verse")[:2] == (61.0, 120.0)
    assert budget("status-bot-v2", "saudi")[:2] == (32.0, 58.0)
