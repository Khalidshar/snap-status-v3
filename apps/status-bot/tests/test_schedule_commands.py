"""WhatsApp's own schedule commands (owner 21 Nov 2026: the old bot's schedule, in V3)."""

from status_bot import owner_commands as oc


def test_pause_resume_and_show_the_whatsapp_schedule(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "STATE", tmp_path)
    assert "أوقفت جدولة واتساب" in oc.answer("⏸ إيقاف الجدولة", None)
    assert (tmp_path / "SCHEDULE_PAUSED").exists()
    assert "⏸ موقوفة" in oc.answer("مواعيد الجدولة", None)
    assert "رجعت جدولة واتساب" in oc.answer("▶️ تشغيل الجدولة", None)
    assert not (tmp_path / "SCHEDULE_PAUSED").exists()
    text = oc.answer("مواعيد الجدولة", None)
    assert "▶️ شغالة" in text and "بعد الفجر" in text
