"""WhatsApp one-step content delivery, identity binding, isolated histories and shared render."""

import hashlib
import json
import subprocess
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import test_status_whatsapp_outbox as fixtures

from snap_bot import command_cards
from snap_bot.did_you_know import KINDS as SHARED_KINDS
from status_bot import content_clips, panel_actions
from status_bot.whatsapp_contract import DeliveryBlocked


@pytest.fixture
def fixture():
    case = fixtures.WhatsAppContracts()
    case.setUp()
    try:
        case.runtime = SimpleNamespace(locked=lambda: nullcontext(), append=Mock())
        with case.repo.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO whatsapp_windows VALUES(?,?)",
                (case.identity.binding, case.repo.clock()),
            )
        yield case
    finally:
        case.doCleanups()


@pytest.mark.parametrize("key", content_clips.KINDS)
def test_every_telegram_kind_is_available_in_whatsapp(key):
    from studio_core.production.model import TITLES

    # «حدث اليوم» exists only in the legacy production path, never in the shared catalogs.
    assert content_clips.KINDS[key] in SHARED_KINDS or (key == "history" and key in TITLES)


def test_video_send_is_once_pinned_and_no_text_reply(fixture, tmp_path):
    f = fixture
    transport = fixtures.OfflineTransport(f.identity)
    content = {"type": "did_you_know", "body": "نص عربي", "kind": "verse"}
    result = {"video_sha256": hashlib.sha256(b"video").hexdigest()}
    item = {"tag": "a" * 64}
    with (
        patch.object(content_clips, "build", return_value=(content, result, b"video")),
        patch.object(content_clips, "valid", return_value=True),
        patch.object(content_clips, "complete"),
    ):
        content_clips.send(f.outbox, transport, item, "verse", f.runtime, root=tmp_path)
        with pytest.raises(DeliveryBlocked, match="ALREADY_CONSUMED"):
            content_clips.send(f.outbox, transport, item, "verse", f.runtime, root=tmp_path)
    transport.upload_video.assert_called_once_with(b"video")
    transport.send_video.assert_called_once()
    with f.repo.connect() as db:
        row = dict(db.execute("SELECT * FROM command_videos").fetchone())
    assert row["state"] == "API_ACCEPTED"
    assert f.identity.recipient not in repr(row)
    f.runtime.append.assert_called_once()


def test_unknown_send_never_retries(fixture, tmp_path):
    f = fixture
    transport = fixtures.OfflineTransport(f.identity)
    transport.send_video.side_effect = TimeoutError("fixture-sensitive-detail")
    with (
        patch.object(
            content_clips, "build", return_value=({}, {"video_sha256": "a" * 64}, b"video")
        ),
        patch.object(content_clips, "valid", return_value=True),
        patch.object(content_clips, "complete"),
    ):
        with pytest.raises(TimeoutError):
            content_clips.send(
                f.outbox, transport, {"tag": "b" * 64}, "hadith", f.runtime, root=tmp_path
            )
        with pytest.raises(DeliveryBlocked):
            content_clips.send(
                f.outbox, transport, {"tag": "b" * 64}, "hadith", f.runtime, root=tmp_path
            )
    assert transport.send_video.call_count == 1
    with f.repo.connect() as db:
        row = dict(db.execute("SELECT * FROM command_videos").fetchone())
    assert row["state"] == "BLOCKED_OR_UNKNOWN"
    assert "sensitive" not in repr(row)


def test_foreign_recipient_or_closed_window_does_not_upload(fixture):
    f = fixture
    transport = fixtures.OfflineTransport(f.identity)
    transport.credentials = SimpleNamespace(
        phone_number=f.identity.phone_number, recipient="19999999999"
    )
    with pytest.raises(DeliveryBlocked, match="IDENTITY"):
        content_clips.send(f.outbox, transport, {"tag": "c" * 64}, "riddle", f.runtime)
    transport.credentials.recipient = f.identity.recipient
    with f.repo.transaction() as db:
        db.execute("UPDATE whatsapp_windows SET last_inbound_at=?", (f.repo.clock() - 86400,))
    with pytest.raises(DeliveryBlocked, match="WINDOW_CLOSED"):
        content_clips.send(f.outbox, transport, {"tag": "c" * 64}, "riddle", f.runtime)
    transport.upload_video.assert_not_called()


def test_real_shared_render_and_twelve_owned_status_agents(fixture, tmp_path):
    f = fixture
    content_clips.initialize(f.repo)
    item = {"tag": "d" * 64}
    with f.repo.transaction() as db:
        db.execute(
            "INSERT INTO command_videos(tag,binding,kind,state,at) VALUES(?,?,?,'WORKING',?)",
            (item["tag"], f.identity.binding, "riddle", f.repo.clock()),
        )
    settings = command_cards.profile()
    settings["ffmpeg_version"] = subprocess.run(
        [settings["ffmpeg"], "-version"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    isolation = dict.fromkeys(("ipv4", "ipv6", "udp", "egress_unreachable"), True)

    def start(unit):
        assert unit == "madar-v3-status-content@" + item["tag"] + ".service"
        content_clips.render_event(item["tag"], tmp_path)

    with (
        patch.object(command_cards, "profile", return_value=settings),
        patch("snap_bot.render.proof", return_value=isolation),
    ):
        content, result, video = content_clips.build(
            f.outbox, item, "riddle", root=tmp_path, start=start
        )
    assert content["kind"] == "riddle" and len(video) > 10000
    assert result["qa"]["duration"] == 8
    from dataclasses import replace

    from studio_core.jobs import Repository

    repo = Repository(replace(f.repo.config, data_root=tmp_path / item["tag"] / "state"))
    with repo.connect() as db:
        rows = db.execute("SELECT app,role,status FROM agent_runs").fetchall()
    assert len(rows) == 12
    assert {r["app"] for r in rows} == {"status-bot-v2"}
    assert {r["status"] for r in rows} == {"SUCCEEDED"}
    assert {r["role"] for r in rows} == set(content_clips.CHAIN)


def test_status_history_blocks_a_previous_fact_in_next_isolated_render(fixture, tmp_path):
    f = fixture
    content_clips.initialize(f.repo)
    isolation = dict.fromkeys(("ipv4", "ipv6", "udp", "egress_unreachable"), True)

    def start(unit):
        tag = unit.split("@")[1].removesuffix(".service")
        directory = tmp_path / tag
        request = json.loads((directory / "request.json").read_bytes())
        (directory / "video.mp4").write_bytes(b"video")
        (directory / "result.json").write_text(
            json.dumps(
                {
                    "video_path": "video.mp4",
                    "video_sha256": hashlib.sha256(b"video").hexdigest(),
                    "content_hash": request["content"]["content_hash"],
                    "network_isolation": isolation,
                    "safe_area": {"result": "PASS"},
                }
            )
        )

    seen = []
    with patch.object(content_clips, "check_voice"), patch.object(content_clips, "check_media"):
        for tag in ("e" * 64, "f" * 64):
            with f.repo.transaction() as db:
                db.execute(
                    "INSERT INTO command_videos(tag,binding,kind,state,at) VALUES(?,?,?,'WORKING',?)",
                    (tag, f.identity.binding, "riddle", f.repo.clock()),
                )
            value, _, _ = content_clips.build(
                f.outbox, {"tag": tag}, "riddle", root=tmp_path, start=start
            )
            seen.append(value["fact_id"])
            with f.repo.transaction() as db:
                db.execute(
                    "UPDATE command_videos SET state='API_ACCEPTED',acked_at=? WHERE tag=?",
                    (f.repo.clock(), tag),
                )
    assert len(set(seen)) == 2


@pytest.mark.parametrize(
    "key",
    sorted(set(content_clips.KINDS) | {"urgent", "saudi", "world", "news", "football", "markets"}),
)
def test_authenticated_content_command_sends_only_one_clip(fixture, key):
    import hmac
    import time

    from status_bot.owner_commands import process_one
    from studio_core.commands import BY_KEY

    f = fixture
    raw, _ = f.webhook(inbound=True)
    value = json.loads(raw)
    message = value["entry"][0]["changes"][0]["value"]["messages"][0]
    message.update(id="wamid.command" + key, timestamp=str(int(time.time())))
    message["text"]["body"] = BY_KEY[key].label
    raw = json.dumps(value, ensure_ascii=False).encode()
    signature = "sha256=" + hmac.new(f.identity.webhook_key, raw, hashlib.sha256).hexdigest()
    f.outbox.webhook(raw, signature)
    transport = fixtures.OfflineTransport(f.identity)
    transport.send_text = Mock()

    def news_reply(service, key, tag, text, clip):
        clip({"command": key})
        return True

    with (
        patch("status_bot.owner_commands.content_clips.send") as send,
        patch("status_bot.owner_commands.command_news.reply", side_effect=news_reply),
    ):
        assert process_one(f.outbox, transport, Mock(), f.runtime)
    send.assert_called_once()
    assert send.call_args.args[3] == key
    transport.send_text.assert_not_called()


def test_window_rechecked_after_upload_and_halt_is_independent(fixture, tmp_path):
    f = fixture
    transport = fixtures.OfflineTransport(f.identity)
    original_upload = transport.upload_video.return_value

    def upload(video):
        with f.repo.transaction() as db:
            db.execute("UPDATE whatsapp_windows SET last_inbound_at=?", (f.repo.clock() - 86400,))
        return original_upload

    transport.upload_video.side_effect = upload
    with (
        patch.object(content_clips, "STATE", tmp_path),
        patch.object(
            content_clips, "build", return_value=({}, {"video_sha256": "a" * 64}, b"video")
        ),
        patch.object(content_clips, "valid", return_value=True),
    ):
        with pytest.raises(DeliveryBlocked, match="WINDOW_CLOSED"):
            content_clips.send(f.outbox, transport, {"tag": "9" * 64}, "verse", f.runtime)
        (tmp_path / "HALT").touch()
        with pytest.raises(DeliveryBlocked, match="SEND_HALTED"):
            content_clips.send(f.outbox, transport, {"tag": "8" * 64}, "verse", f.runtime)
    transport.send_video.assert_not_called()


@pytest.mark.parametrize(
    ("failure_stage", "error", "state", "code"),
    [
        ("upload_video", DeliveryBlocked("WA_WINDOW_CLOSED"), "BLOCKED", "WA_WINDOW_CLOSED"),
        ("send_video", ConnectionResetError("private detail"), "BLOCKED_OR_UNKNOWN", "OS_ERROR"),
        (
            "send_video",
            DeliveryBlocked("WA_TRANSPORT_UNKNOWN"),
            "BLOCKED_OR_UNKNOWN",
            "WA_TRANSPORT_UNKNOWN",
        ),
    ],
)
def test_panel_approval_failure_closes_attempt_and_only_reopens_before_send(
    fixture, tmp_path, monkeypatch, failure_stage, error, state, code
):
    f = fixture
    transport = fixtures.OfflineTransport(f.identity)
    path = tmp_path / "panel.sqlite"
    preview, tag = "1" * 32, "2" * 64
    workspace = panel_actions.workspace
    sha = workspace.record_preview(path, preview, "verse", b"video", [])
    panel_actions.media(path, preview, ".json").write_text(
        json.dumps({"tag": tag, "command": "verse", "content": {}, "result": {"video_sha256": sha}})
    )
    monkeypatch.setattr(content_clips, "complete", Mock())
    monkeypatch.setattr(content_clips, "valid", Mock(return_value=True))
    failing = getattr(transport, failure_stage)
    failing.side_effect = error
    payload = {"preview": preview, "sha": sha}
    assert workspace.submit(path, "3" * 32, "approve", payload)
    assert panel_actions.process(f.outbox, transport, f.runtime, path)
    with f.repo.connect() as db:
        assert tuple(db.execute("SELECT state,code FROM command_videos").fetchone()) == (
            state,
            code,
        )
    failing.side_effect = None
    if state == "BLOCKED":
        transport.send_video.assert_not_called()
        assert workspace.submit(path, "4" * 32, "approve", payload)
        assert panel_actions.process(f.outbox, transport, f.runtime, path)
        with f.repo.connect() as db:
            assert tuple(db.execute("SELECT state,code FROM command_videos").fetchone()) == (
                "API_ACCEPTED",
                None,
            )
    else:
        with pytest.raises(ValueError, match="PREVIEW_NOT_REVIEWABLE"):
            workspace.submit(path, "4" * 32, "approve", payload)
        with pytest.raises(DeliveryBlocked, match="WA_COMMAND_ALREADY_CONSUMED"):
            panel_actions.approve(f.outbox, transport, f.runtime, path, preview, sha)
    transport.send_video.assert_called_once()
