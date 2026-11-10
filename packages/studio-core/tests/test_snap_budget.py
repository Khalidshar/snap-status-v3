"""Every Snap clip keeps a margin over the one-minute Spotlight floor (owner 10 Nov 2026)."""

from studio_core.production.model import NEWS, QURAN, SACRED, budget


def test_every_snap_clip_at_least_65_seconds():
    for key in NEWS | QURAN | SACRED | {"facts", "tech", "style", "riddle", "funny", "history"}:
        assert budget("snap-bot-v2", key)[0] == 65.0


def test_whatsapp_unchanged():
    assert budget("status-bot-v2", "saudi")[:2] == (32.0, 58.0)
