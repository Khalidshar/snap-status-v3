"""Written-news replay, app separation and uncertain-response reservation tests."""

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from studio_core.network.client import NetworkResponse
from studio_core.sources.live import LiveNews

ROOT = Path(__file__).resolve().parents[3]


class Feed:
    def get(self, url):
        from urllib.parse import urlsplit

        host = urlsplit(url).netloc
        host = "www.bbc.com" if host == "feeds.bbci.co.uk" else host
        date = datetime.fromtimestamp(time.time() - 60, UTC).isoformat()
        raw = (
            "<rss><channel><item><title>الرياض تعلن مبادرة سعودية جديدة</title>"
            f"<link>https://{host}/one</link><pubDate>{date}</pubDate>"
            "</item></channel></rss>"
        ).encode()
        return NetworkResponse(200, "text/xml", raw, hashlib.sha256(raw).hexdigest())


def news(tmp_path, app="snap"):
    return LiveNews(app + "-bot", ROOT / f"config/{app}/weather.toml", tmp_path / "history.sqlite3")


def test_reply_copies_source_text_and_id_is_durable_after_reopen(tmp_path):
    send = Mock()
    assert news(tmp_path).reply("saudi", "event1", send, client=Feed())
    assert not news(tmp_path).reply("saudi", "event1", send, client=Feed())
    send.assert_called_once()
    text = send.call_args.args[0]
    assert "الرياض تعلن مبادرة سعودية جديدة" in text
    assert "المصدر:" in text and "https://" in text


def test_unknown_send_reserves_headline_and_never_retries(tmp_path):
    send = Mock(side_effect=TimeoutError("fixture"))
    with pytest.raises(TimeoutError):
        news(tmp_path).reply("saudi", "event1", send, client=Feed())
    assert not news(tmp_path).reply("saudi", "event1", send, client=Feed())
    second = Mock()
    assert news(tmp_path).reply("saudi", "event2", second, client=Feed())
    assert "لا يوجد خبر عربي" in second.call_args.args[0]
    assert send.call_count == 1


def test_concurrent_same_update_emits_only_one_external_reply(tmp_path):
    service = news(tmp_path)
    send = Mock()
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda _: service.reply("saudi", "same", send, client=Feed()), range(2))
        )
    assert sorted(results) == [False, True]
    send.assert_called_once()


def test_app_and_symlink_history_are_rejected(tmp_path):
    news(tmp_path)
    with pytest.raises(ValueError, match="Foreign"):
        news(tmp_path, "status")
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="Unsafe"):
        news(link)
