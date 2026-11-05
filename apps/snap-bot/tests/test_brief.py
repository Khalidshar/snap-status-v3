"""Frozen approved faith, two fresh source headlines, one idempotent requested brief."""

import copy
import hashlib
import json
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from snap_bot import brief, owner_commands
from studio_core.network.client import NetworkResponse
from studio_core.sources.live import LiveNews
from studio_core.sources.news import digest

ROOT = Path(__file__).resolve().parents[3]


class Feed:
    def get(self, url):
        host = url.split("/")[2]
        title = (
            "تقنية جديدة للحواسيب"
            if any(
                w in host for w in ("aitnews", "tech-wd", "unlimit-tech", "youm7", "arabhardware")
            )
            else "الرياض تعلن مبادرة سعودية جديدة"
        )
        date = datetime.fromtimestamp(time.time() - 20, UTC).isoformat()
        raw = f"<rss><channel><item><title>{title}</title><link>https://{host}/one</link><pubDate>{date}</pubDate></item></channel></rss>".encode()
        return NetworkResponse(200, "text/xml", raw, hashlib.sha256(raw).hexdigest())


def service(root):
    return LiveNews("snap-bot", ROOT / "config/snap/news.toml", root / "history.sqlite3")


def test_dates_use_riyadh_civil_day_and_umm_al_qura():
    at = datetime(2023, 7, 18, 22, 0, tzinfo=UTC).timestamp()
    gregorian, hijri = brief.dates(at)
    assert "١٩" in gregorian and "يوليو" in gregorian
    assert hijri == "١ محرم ١٤٤٥ هـ"


def test_prepared_brief_is_immutable_and_deduplicates_both_news(tmp_path):
    svc = service(tmp_path)
    value = brief.prepare(svc, "1", client=Feed())
    brief.validate_brief(value, time.time())
    assert value["saudi"]["command"] == "saudi" and value["tech"]["command"] == "tech"
    assert value["faith"]["kind"] == "verse"
    assert brief.prepare(svc, "1", client=Feed()) is None
    for key in ("faith", "saudi", "tech", "dates"):
        bad = copy.deepcopy(value)
        if key == "faith":
            bad[key]["body"] += "ا"
        elif key == "dates":
            bad[key][0] = "تاريخ آخر"
        else:
            bad[key]["headline"] += " آخر"
        bad["content_hash"] = digest(bad)
        with pytest.raises(ValueError):
            brief.validate_brief(bad, time.time())
    with svc.connect() as db:
        assert (
            db.execute("SELECT count(*) FROM replies WHERE fingerprint IS NOT NULL").fetchone()[0]
            == 2
        )


def test_reply_only_emits_one_video_and_records_ack(tmp_path):
    svc = service(tmp_path)
    runtime = SimpleNamespace(locked=lambda: nullcontext(), append=Mock())
    text, video = Mock(), Mock()
    raw = b"fixture-video"

    def start(unit):
        tag = unit.split("@")[1].removesuffix(".service")
        path = tmp_path / "out" / tag
        value = json.loads((path / "content.json").read_bytes())
        (path / "video.mp4").write_bytes(raw)
        (path / "result.json").write_text(
            json.dumps(
                {
                    "content_hash": value["content_hash"],
                    "video_sha256": hashlib.sha256(raw).hexdigest(),
                    "duration": 32,
                }
            )
        )

    args = svc, "1", text, video, runtime, tmp_path / "HALT"
    brief.reply(*args, root=tmp_path / "out", client=Feed(), start=start)
    brief.reply(*args, root=tmp_path / "out", client=Feed(), start=start)
    video.assert_called_once_with(raw)
    text.assert_not_called()
    with svc.connect() as db:
        assert {r[0] for r in db.execute("SELECT state FROM replies")} == {"ACK"}


def test_no_recent_news_and_halt_do_not_send_video(tmp_path):
    from studio_core.sources.news import NewsUnavailable

    svc = service(tmp_path)
    video, text = Mock(), Mock()
    with patch.object(brief, "prepare", side_effect=NewsUnavailable()):
        brief.reply(svc, "1", text, video, Mock(), tmp_path / "HALT")
    assert "لا يوجد خبر" in text.call_args.args[0]
    (tmp_path / "HALT").touch()
    brief.reply(svc, "2", text, video, Mock(), tmp_path / "HALT")
    video.assert_not_called()


def test_telegram_command_routes_to_brief_without_text_reply(tmp_path):
    update = {
        "update_id": 1,
        "message": {
            "date": int(time.time()),
            "text": "يومك في دقيقة",
            "chat": {"id": 77, "type": "private"},
            "from": {"id": 77},
        },
    }
    transport = Mock(return_value=[update])
    with (
        patch.object(owner_commands, "live_news", return_value=Mock()),
        patch.object(brief, "reply") as reply,
    ):
        owner_commands.poll_once("t", "77", SimpleNamespace(runtime=Mock()), 0, transport)
    reply.assert_called_once()
    assert transport.call_count == 1


def test_real_complete_brief_is_one_video_under_a_minute(tmp_path):
    import shutil
    import subprocess

    from snap_bot import command_cards

    pango = shutil.which("pango-view")
    assert pango, "Install Linux Pango before running the real brief test"
    value = brief.prepare(service(tmp_path), "real", client=Feed())
    tag = "a" * 64
    directory = tmp_path / "out" / tag
    directory.mkdir(parents=True)
    (directory / "content.json").write_text(json.dumps(value))
    settings = command_cards.profile()
    settings["ffmpeg_version"] = subprocess.run(
        [settings["ffmpeg"], "-version"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    settings["pango_view"] = pango
    settings["pango_version"] = subprocess.run(
        [pango, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    isolation = dict.fromkeys(("ipv4", "ipv6", "udp", "egress_unreachable"), True)
    with (
        patch("snap_bot.command_cards.profile", return_value=settings),
        patch("snap_bot.command_cards.proof", return_value=isolation),
        patch("snap_bot.render.proof", return_value=isolation),
    ):
        brief.render_event(tag, tmp_path / "out")
    result = json.loads((directory / "result.json").read_bytes())
    assert 30 <= result["duration"] <= 60
    assert result["content_hash"] == value["content_hash"]
    assert (
        result["video_sha256"] == hashlib.sha256((directory / "video.mp4").read_bytes()).hexdigest()
    )


def test_calendar_table_is_pinned_and_rejects_corruption(tmp_path):
    from datetime import date

    from snap_bot import civil_calendar

    assert civil_calendar.hijri(date(2023, 7, 19)) == (1445, 1, 1)
    assert civil_calendar.hijri(date(1924, 8, 1)) == (1343, 1, 1)
    with pytest.raises(OverflowError):
        civil_calendar.hijri(date(2078, 1, 1))
    changed = tmp_path / "calendar.json"
    changed.write_bytes(civil_calendar.DATA.read_bytes().replace(b"23999", b"23998", 1))
    with (
        patch.object(civil_calendar, "DATA", changed),
        pytest.raises(ValueError, match="table changed"),
    ):
        civil_calendar.hijri(date(2023, 7, 19))
