"""The video news adapter keeps the existing written-news reservations."""

import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from studio_core.command_news import reply
from studio_core.network import NetworkResponse
from studio_core.sources.live import LiveNews

ROOT = Path(__file__).resolve().parents[3]


class Feed:
    def get(self, url):
        host = url.split("/")[2]
        at = datetime.fromtimestamp(time.time() - 20, UTC).isoformat()
        raw = (
            "<rss><channel><item><title>الرياض تعلن مبادرة سعودية جديدة</title>"
            f"<link>https://{host}/one</link><pubDate>{at}</pubDate>"
            "</item></channel></rss>"
        ).encode()
        return NetworkResponse(200, "text/xml", raw, hashlib.sha256(raw).hexdigest())


def test_video_ack_and_uncertain_reply_remain_reserved(tmp_path):
    service = LiveNews("status-bot", ROOT / "config/status/news.toml", tmp_path / "history.sqlite3")
    text, clip = Mock(), Mock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):
        reply(service, "saudi", "1", text, clip, client=Feed())
    assert not reply(service, "saudi", "1", text, clip, client=Feed())
    clip.assert_called_once()
    reply(service, "saudi", "2", text, clip, client=Feed())
    assert "لا يوجد خبر عربي" in text.call_args.args[0]
