"""One-step live news: exact text, scoped history, pixels, isolated render, confirmed video."""

import hashlib
import json
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from snap_bot import command_cards, news_clips, owner_commands
from studio_core import command_news
from studio_core.network.client import NetworkResponse
from studio_core.sources import for_command
from studio_core.sources.live import LiveNews
from studio_core.sources.news import TITLES, collect

ROOT = Path(__file__).resolve().parents[3]
ISOLATION = dict.fromkeys(("ipv4", "ipv6", "udp", "egress_unreachable"), True)
HEADLINES = {
    "urgent": "عاجل قصف عسكري جديد",
    "saudi": "الرياض تعلن مبادرة سعودية جديدة",
    "world": "فرنسا تعلن مبادرة جديدة",
    "news": "تطور جديد في قطاع الخدمات",
    "football": "مباراة جديدة في دوري روشن",
    "markets": "الأسواق تسجل ارتفاعا جديدا",
    "tech": "تقنية جديدة للحواسيب",
    "style": "عطر جديد ومكياج جديد",
}


class Feed:
    def __init__(self, key):
        self.key = key

    def get(self, url):
        source = next(s for s in for_command(self.key) if s.url == url)
        host = source.url.split("/")[2]
        title = HEADLINES[self.key] + (" - مجلة هي" if source.id.startswith("gnews-") else "")
        date = datetime.fromtimestamp(time.time() - 30, UTC).isoformat()
        raw = f"<rss><channel><item><title>{title}</title><link>https://{host}/one</link><pubDate>{date}</pubDate></item></channel></rss>".encode()
        return NetworkResponse(200, "text/xml", raw, hashlib.sha256(raw).hexdigest())


def service(tmp):
    return LiveNews("snap-bot", ROOT / "config/snap/news.toml", tmp / "history.sqlite3")


@pytest.mark.parametrize("key", TITLES)
def test_each_command_delivers_clip_only_with_original_headline(tmp_path, key, monkeypatch):
    # A frozen request clock isolates the saved-news contract from wall-clock drift.
    request_time = time.time()
    monkeypatch.setattr(time, "time", lambda: request_time)
    text, clip = Mock(), Mock()
    svc = service(tmp_path)
    assert command_news.reply(svc, key, "one", text, clip, client=Feed(key))
    text.assert_not_called()
    value = clip.call_args.args[0]
    assert value["headline"] == HEADLINES[key]
    rows = news_clips.labels(value)
    assert "".join(r[0] for r in rows[1:-2]) == value["headline"]
    assert rows[-2][0] == "المصدر: " + value["source"]
    assert rows[-1][0] == value["published_label"]
    assert not command_news.reply(svc, key, "one", text, clip, client=Feed(key))
    clip.assert_called_once()
    command_news.reply(svc, key, "two", text, clip, client=Feed(key))
    assert "لا يوجد خبر عربي" in text.call_args.args[0]
    clip.assert_called_once()


def test_unknown_video_send_is_not_replayed(tmp_path):
    svc = service(tmp_path)
    clip = Mock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):
        command_news.reply(svc, "saudi", "one", Mock(), clip, client=Feed("saudi"))
    assert not command_news.reply(svc, "saudi", "one", Mock(), clip, client=Feed("saudi"))
    with svc.connect() as db:
        assert db.execute("SELECT state FROM replies").fetchone()[0] == "UNKNOWN"


def test_listener_routes_live_news_to_video_and_honors_halt(tmp_path):
    svc = service(tmp_path)
    update = {
        "update_id": 12,
        "message": {
            "date": int(time.time()),
            "text": "السعودية",
            "chat": {"id": 77, "type": "private"},
            "from": {"id": 77},
        },
    }
    calls = []

    def transport(token, method, body=b"", kind=None):
        calls.append(method)
        return [update] if method == "getUpdates" else {"message_id": 15, "chat": {"id": 77}}

    original = command_news.reply

    def reply(service, key, event, text, clip):
        return original(service, key, event, text, clip, client=Feed(key))

    halt = tmp_path / "HALT"
    with (
        patch.dict("os.environ", {"MADAR_LIVE_NEWS": "1"}),
        patch.object(owner_commands, "HALT", halt),
        patch.object(owner_commands, "live_news", return_value=svc),
        patch.object(owner_commands.command_news, "reply", side_effect=reply),
        patch.object(owner_commands.news_clips, "deliver") as deliver,
    ):
        # Preserve the real adapter before patching its name in the listener.
        owner_commands.poll_once("t", "77", SimpleNamespace(runtime=Mock()), 0, transport)
        deliver.assert_called_once()
        assert calls == ["getUpdates"]
        halt.touch()
        owner_commands.poll_once("t", "77", SimpleNamespace(runtime=Mock()), 0, transport)
        assert calls[-1] == "sendMessage"
        assert deliver.call_count == 1


def test_delivery_requires_digest_and_owner_ack(tmp_path):
    value = collect("saudi", Feed("saudi"), time.time())["content"]
    runtime = SimpleNamespace(locked=lambda: nullcontext(), append=Mock())
    video = b"fixture-video"

    def start(unit):
        tag = unit.split("@", 1)[1].removesuffix(".service")
        directory = tmp_path / tag
        (directory / "render").mkdir()
        (directory / "render/video.mp4").write_bytes(video)
        (directory / "result.json").write_text(
            json.dumps(
                {
                    "video_sha256": hashlib.sha256(video).hexdigest(),
                    "content_hash": value["content_hash"],
                    "network_isolation": ISOLATION,
                    "safe_area": {"result": "PASS"},
                }
            )
        )

    send = Mock(return_value={"message_id": 7, "chat": {"id": 77}})
    news_clips.deliver(
        value, "one", "t", "77", send, runtime, tmp_path / "HALT", root=tmp_path, start=start
    )
    assert send.call_args.args[1] == "sendVideo"
    runtime.append.assert_called_once()
    send.reset_mock()

    def changed(unit):
        start(unit)
        tag = unit.split("@", 1)[1].removesuffix(".service")
        (tmp_path / tag / "render/video.mp4").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="admission"):
        news_clips.deliver(
            value, "two", "t", "77", send, runtime, tmp_path / "HALT", root=tmp_path, start=changed
        )
    send.assert_not_called()


def test_real_card_pixel_bounds_and_silent_video(tmp_path):
    settings = command_cards.profile()
    import subprocess

    settings["ffmpeg_version"] = subprocess.run(
        [settings["ffmpeg"], "-version"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    value = collect("saudi", Feed("saudi"), time.time())["content"]
    with patch("snap_bot.command_cards.proof", return_value=ISOLATION):
        result = command_cards.render(
            news_clips.labels(value), tmp_path / "card", settings=settings
        )
    assert result["duration"] == 8
    assert result["safe_area"]["result"] == "PASS"
    assert result["audio_policy"] == "SILENT"
    assert (tmp_path / "card/preview.png").is_file()


def test_legacy_text_reply_history_also_blocks_video(tmp_path):
    svc = service(tmp_path)
    svc.reply("saudi", "old", Mock(), client=Feed("saudi"))
    text, clip = Mock(), Mock()
    command_news.reply(svc, "saudi", "new", text, clip, client=Feed("saudi"))
    clip.assert_not_called()
    assert "لا يوجد خبر عربي" in text.call_args.args[0]


def test_concurrent_event_reserves_only_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    svc = service(tmp_path)
    clip = Mock()
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda _: command_news.reply(
                    svc, "saudi", "same", Mock(), clip, client=Feed("saudi")
                ),
                range(2),
            )
        )
    assert sorted(results) == [False, True]
    clip.assert_called_once()
