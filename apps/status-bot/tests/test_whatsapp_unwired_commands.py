"""Every WhatsApp menu command now has an answer (owner 21 Nov 2026)."""

from status_bot import owner_commands as oc


def test_preview_send_cancel_dash_and_add_fact_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "STATE", tmp_path)
    for text in ("👁 معاينة", "✅ أرسل", "❌ ألغِ"):
        assert "تخص بوت سناب" in oc.answer(text, None), text
    assert "لوحة التحكم" in oc.answer("🖥️ لوحة التحكم", None)
    from studio_core.commands import BY_KEY

    assert "أضف هل تعلم:" in oc.answer(BY_KEY["add_fact"].label, None)
    assert "قائمة المراجعة" in oc.answer("أضف هل تعلم: النحل يرقص ليدل على الزهور", None)
    assert (tmp_path / "owner-facts.jsonl").is_file()


def test_no_menu_command_is_left_without_a_path():
    from studio_core.commands import COMMANDS

    handled_elsewhere = {"halt", "unhalt", "brief", "agents", "menu"}
    for command in COMMANDS:
        if command.key in handled_elsewhere:
            continue
        reply = oc.answer(command.label, None) if command.section in {"control", "help"} else ""
        assert "لا يملك مسار" not in reply, command.key
