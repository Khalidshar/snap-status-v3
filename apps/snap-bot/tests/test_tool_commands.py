"""Snap's six agent tools read Snap's own evidence and change nothing (owner 21 Nov 2026)."""

import json
import time

import pytest

from snap_bot import tool_commands as tools


def preview(root, pid, key, scenes):
    folder = root / "previews" / pid / "state" / "renders" / "r1" / "input"
    folder.mkdir(parents=True)
    value = {"title": "t", "kind": key, "legacy_production": {"key": key, "scenes": scenes}}
    (folder / "content.json").write_text(json.dumps(value, ensure_ascii=False))


def setup(tmp_path):
    now = time.time()
    preview(
        tmp_path,
        "p1",
        "saudi",
        [
            {"head": "خبر سعودي قصير", "body": "نص كامل.", "image": "a.jpg", "image_credit": "x"},
            {"head": "ع" * 90, "body": "ملخص مقطوع…", "image": "b.jpg"},
        ],
    )
    preview(tmp_path, "p2", "verse", [{"body": "آية"}])
    failure = tmp_path / "previews" / "p2" / "server-failure.json"
    failure.write_text(json.dumps({"reason": "SERVER_SEND_FAILED"}))
    events = [
        {"kind": "RENDERED", "preview_id": "p1", "video_sha": "s1", "time": now - 60},
        {"kind": "ACK", "video_sha": "s1", "time": now - 50},
        {"kind": "RENDERED", "preview_id": "p2", "video_sha": "s2", "time": now - 40},
        {"kind": "PRE_NETWORK_BLOCKED", "video_sha": "s2", "time": now - 30},
    ]
    return events


def test_every_tool_answers_and_nothing_is_written_but_its_status(tmp_path):
    events = setup(tmp_path)
    before = sorted(p for p in tmp_path.rglob("*") if p.name != "agent-status.sqlite")
    for key in tools.NAMES:
        reply = tools.execute(key, tmp_path, events)
        assert reply.startswith("🤖 " + tools.NAMES[key])
        assert "تعذر" not in reply, key
    after = sorted(p for p in tmp_path.rglob("*") if p.name != "agent-status.sqlite")
    assert before == after  # read-only: no clip, file or evidence changed


def test_manager_analyst_and_repair_read_the_event_log(tmp_path):
    events = setup(tmp_path)
    assert "جاهز لتلقي الأوامر" in tools.execute("manager", tmp_path, events)
    assert "ينتظر موافقتك: آية" in tools.execute("manager", tmp_path, events, {"title": "آية"})
    analyst = tools.execute("analyst", tmp_path, events)
    assert "صُنع ٢ · أُرسل ١ · انمنع ١" in analyst and "أخبار السعودية: ١" in analyst
    repair = tools.execute("content_repair", tmp_path, events)
    assert "انمنع ١ إرسال" in repair and "SERVER_SEND_FAILED" in repair


@pytest.mark.real_chain_gate
def test_writer_reviewer_and_fixer_use_the_latest_clip(tmp_path):
    events = setup(tmp_path)
    # The latest clip is a verse: sacred text is locked for the fixer.
    assert "مقفل" in tools.execute("fixer", tmp_path, events)
    assert "آية اليوم" in tools.execute("writer", tmp_path, events)
    # Without the verse, the Saudi bulletin is the latest clip.
    news = events[:2]
    assert "خبر سعودي قصير" in tools.execute("writer", tmp_path, news)
    fixer = tools.execute("fixer", tmp_path, news)
    assert "العنوان طويل" in fixer and "مقطوع" in fixer and "بلا مصدر" in fixer
    reviewer = tools.execute("reviewer", tmp_path, news)
    assert "❌" in reviewer  # no agent ledger in this fixture: never claimed as passed


def test_no_clip_yet_is_said_plainly(tmp_path):
    assert "ما فيه مقطع صالح" in tools.execute("writer", tmp_path, [])


def test_add_fact_in_snap_joins_the_review_queue(tmp_path):
    from studio_core.observability.agent_tools import add_fact, queued_facts

    assert "قائمة المراجعة" in add_fact(tmp_path, "قلب الحوت الأزرق بحجم سيارة صغيرة")
    assert len(queued_facts(tmp_path)) == 1
    events = setup(tmp_path)
    assert "تنتظر التحقق" in tools.execute("manager", tmp_path, events)
