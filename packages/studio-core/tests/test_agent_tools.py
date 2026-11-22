"""Shared agent-tool wording and the owner's fact queue (owner 21 Nov 2026)."""

from studio_core.observability import agent_tools as tools


def test_fact_queue_appends_once_and_never_publishes(tmp_path):
    reply = tools.add_fact(tmp_path, "  الأخطبوط له   ثلاثة قلوب  ")
    assert "رقم ١" in reply and "التحقق من مصدرها" in reply
    assert tools.queued_facts(tmp_path)[0]["text"] == "الأخطبوط له ثلاثة قلوب"
    assert "محفوظة من قبل" in tools.add_fact(tmp_path, "الأخطبوط له ثلاثة قلوب")
    assert "ما انحفظ شي" in tools.add_fact(tmp_path, "قصير")
    assert len(tools.queued_facts(tmp_path)) == 1
    assert "تنتظر التحقق من مصدرها: ١" in "\n".join(tools.status_lines(tmp_path))


def test_fixer_never_suggests_on_sacred_text():
    value = {"legacy_production": {"key": "verse", "scenes": [{"head": "ع" * 99}]}}
    assert "مقفل" in tools.fixer(value)
    assert "مقفل" in tools.fixer({"sacred": True})


def test_run_records_failure_and_says_it(tmp_path):
    def broken() -> str:
        raise RuntimeError("boom")

    reply = tools.run("snap-bot-v2", "writer", tmp_path / "agents.sqlite", broken)
    assert reply.startswith("🤖 كاتب المحتوى") and "تعذر" in reply


def test_run_with_no_clip_yet_is_not_a_failure(tmp_path):
    def empty() -> str:
        raise ValueError("NO_CONTENT")

    reply = tools.run("snap-bot-v2", "writer", tmp_path / "agents.sqlite", empty)
    assert "ما فيه مقطع مصنوع بعد" in reply and "تعذر" not in reply


def test_failure_reason_is_only_an_internal_code():
    from studio_core.observability.failure_code import failure_reason

    assert failure_reason(ValueError("LEGACY_BULLETIN_INCOMPLETE")) == "LEGACY_BULLETIN_INCOMPLETE"
    for unsafe in ("/var/lib/madar/x missing", "token=abc", "Invalid source", ""):
        assert failure_reason(ValueError(unsafe)) == ""
