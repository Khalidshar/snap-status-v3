from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from status_bot import daily_brief
from status_bot.whatsapp_contract import DeliveryBlocked


@contextmanager
def connection():
    yield Mock()


def test_brief_uses_existing_video_chain_and_no_duplicate_replay():
    outbox = Mock()
    news = SimpleNamespace(connect=connection)
    transport = Mock()
    item = {"tag": "a" * 64}
    with (
        patch.object(daily_brief, "ensure"),
        patch.object(daily_brief, "halted", return_value=False),
        patch.object(daily_brief, "prepare", side_effect=[{"content_hash": "b" * 64}, None]),
        patch.object(daily_brief, "validate_brief"),
        patch.object(daily_brief.content_clips, "send") as send,
    ):
        assert daily_brief.send(outbox, transport, news, item, Mock())
        assert not daily_brief.send(outbox, transport, news, item, Mock())
        send.assert_called_once()
        assert send.call_args.args[3] == "brief"


def test_halt_or_closed_window_prevents_prepare_and_send():
    with (
        patch.object(daily_brief, "ensure"),
        patch.object(daily_brief, "halted", return_value=True),
        patch.object(daily_brief, "prepare") as prepare,
    ):
        with pytest.raises(ValueError, match="HALTED"):
            daily_brief.send(Mock(), Mock(), Mock(), {"tag": "a" * 64}, Mock())
        prepare.assert_not_called()
    with (
        patch.object(
            daily_brief, "ensure", side_effect=DeliveryBlocked("WA_SERVICE_WINDOW_CLOSED")
        ),
        patch.object(daily_brief, "prepare") as prepare,
    ):
        with pytest.raises(DeliveryBlocked, match="WINDOW_CLOSED"):
            daily_brief.send(Mock(), Mock(), Mock(), {"tag": "a" * 64}, Mock())
        prepare.assert_not_called()


def test_unknown_send_not_retried():
    news = SimpleNamespace(connect=connection)
    with (
        patch.object(daily_brief, "ensure"),
        patch.object(daily_brief, "halted", return_value=False),
        patch.object(daily_brief, "prepare", return_value={}),
        patch.object(daily_brief, "validate_brief"),
        patch.object(
            daily_brief.content_clips, "send", side_effect=TimeoutError("fixture")
        ) as send,
    ):
        with pytest.raises(TimeoutError):
            daily_brief.send(Mock(), Mock(), news, {"tag": "a" * 64}, Mock())
        assert send.call_count == 1


def test_brief_runs_all_twelve_owned_stages(tmp_path):
    import hashlib
    import json
    import time
    from datetime import UTC, datetime
    from pathlib import Path

    import test_status_whatsapp_outbox as fixtures

    from status_bot import content_clips
    from studio_core.network.client import NetworkResponse
    from studio_core.observability.role_status import snapshot
    from studio_core.sources.daily_brief import prepare
    from studio_core.sources.live import LiveNews

    class Feed:
        def get(self, url):
            host = url.split("/")[2]
            title = (
                "تقنية جديدة للحواسيب"
                if any(
                    x in host
                    for x in ("aitnews", "tech-wd", "unlimit-tech", "youm7", "arabhardware")
                )
                else "الرياض تعلن مبادرة سعودية جديدة"
            )
            at = datetime.fromtimestamp(time.time() - 20, UTC).isoformat()
            raw = f"<rss><channel><item><title>{title}</title><link>https://{host}/one</link><pubDate>{at}</pubDate></item></channel></rss>".encode()
            return NetworkResponse(200, "text/xml", raw, hashlib.sha256(raw).hexdigest())

    case = fixtures.WhatsAppContracts()
    case.setUp()
    try:
        news = LiveNews(
            "status-bot",
            Path(__file__).parents[3] / "config/status/news.toml",
            tmp_path / "history.sqlite",
        )
        value = prepare(news, "test", client=Feed())
        item = {"tag": "c" * 64}
        content_clips.initialize(case.repo)
        with case.repo.transaction() as db:
            db.execute(
                "INSERT INTO command_videos(tag,binding,kind,state,at) VALUES(?,?,?,'WORKING',?)",
                (item["tag"], case.identity.binding, "brief", case.repo.clock()),
            )

        def start(unit):
            folder = tmp_path / "clips" / item["tag"]
            request = json.loads((folder / "request.json").read_text())
            assert request["brief"] and request["app"] == "status-bot-v2"
            (folder / "brief").mkdir()
            raw = b"offline-fixture-video"
            (folder / "brief/video.mp4").write_bytes(raw)
            result = {
                "content_hash": value["content_hash"],
                "video_sha256": hashlib.sha256(raw).hexdigest(),
                "video_path": "brief/video.mp4",
                "audio_policy": "BRIEF_MIX",
                "duration": 32,
                "safe_area": {"result": "PASS"},
                "network_isolation": dict.fromkeys(
                    ("ipv4", "ipv6", "udp", "egress_unreachable"), True
                ),
            }
            (folder / "result.json").write_text(json.dumps(result))

        with patch.object(content_clips.footage, "prepare"):
            content, result, video = content_clips.build(
                case.outbox, item, "brief", source=value, root=tmp_path / "clips", start=start
            )
        assert content == value and result["duration"] == 32 and video == b"offline-fixture-video"
        rows = snapshot(case.repo.config.data_root / "agent-status.sqlite", "status-bot-v2")
        assert {r["id"] for r in rows if r["state"] == "SUCCEEDED"} == set(content_clips.CHAIN)
    finally:
        case.doCleanups()
