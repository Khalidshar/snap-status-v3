import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

from studio_core.network.client import NetworkResponse
from studio_core.sources.live import LiveNews
from studio_core.sources.war_alerts import is_war, tick

ROOT = Path(__file__).resolve().parents[3]


class Feed:
    def __init__(self, headline="عاجل: قصف عسكري قرب الحدود"):
        self.headline = headline
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        date = datetime.fromtimestamp(time.time() - 30, UTC).isoformat()
        raw = f"<rss><channel><item><title>{self.headline}</title><link>https://aawsat.com/test</link><pubDate>{date}</pubDate></item></channel></rss>".encode()
        return NetworkResponse(200, "text/xml", raw, hashlib.sha256(raw).hexdigest())


def service(tmp_path):
    return LiveNews("snap-bot", ROOT / "config/snap/news.toml", tmp_path / "news.sqlite")


def test_disabled_halted_and_empty_approvals_never_fetch(tmp_path):
    news, client, send = service(tmp_path), Feed(), Mock()
    assert not tick(news, frozenset({"aawsat"}), lambda: True, send, client=client)
    assert not tick(news, frozenset({"aawsat"}), lambda: False, send, enabled=True, client=client)
    assert not tick(news, frozenset(), lambda: True, send, enabled=True, client=client)
    assert not client.calls
    send.assert_not_called()


def test_alert_once_and_unknown_never_retried(tmp_path):
    news, client = service(tmp_path), Feed()
    send = Mock(side_effect=OSError("fixture"))
    assert not tick(news, frozenset({"aawsat"}), lambda: True, send, enabled=True, client=client)
    assert send.call_count == 1
    assert not tick(news, frozenset({"aawsat"}), lambda: True, send, enabled=True, client=client)
    assert send.call_count == 1
    with news.connect() as db:
        assert db.execute("SELECT state FROM replies").fetchone()[0] == "UNKNOWN"


def test_war_only_and_recheck_stop_before_send(tmp_path):
    news, send = service(tmp_path), Mock()
    assert not tick(
        news,
        frozenset({"aawsat"}),
        lambda: True,
        send,
        enabled=True,
        client=Feed("عاجل: افتتاح مشروع اقتصادي"),
    )
    assert not is_war("عاجل: نتيجة مباراة كرة القدم")
    gates = iter([True, True, False])
    assert not tick(
        news, frozenset({"aawsat"}), lambda: next(gates), send, enabled=True, client=Feed()
    )
    send.assert_not_called()


def test_send_copies_original_source_and_time(tmp_path):
    news, send, client = service(tmp_path), Mock(), Feed()
    assert tick(news, frozenset({"aawsat"}), lambda: True, send, enabled=True, client=client)
    text = send.call_args.args[0]
    assert client.headline in text and "المصدر:" in text and "https://aawsat.com/test" in text
    assert len(client.calls) == 1
