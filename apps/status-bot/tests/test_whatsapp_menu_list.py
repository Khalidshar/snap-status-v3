from status_bot import owner_commands
from studio_core import commands as menu


def test_menu_is_a_tappable_list_of_sections():
    text, action = owner_commands.menu_list("مرحبا")
    rows = action["sections"][0]["rows"]
    assert action["button"] == "القائمة" and "القائمة" in text
    assert [r["title"] for r in rows] == [t[:24] for _, t, _ in menu.SECTIONS]
    brief = menu.section_rows("brief")[0]
    assert {"id": "c:" + str(brief.number), "title": "⏱️ يومك في دقيقة"} in rows


def test_section_opens_its_commands_with_telegram_labels():
    text, action = owner_commands.menu_list("s:news")
    rows = action["sections"][0]["rows"]
    assert text.startswith("📰 أخبار")
    assert [r["title"] for r in rows] == [c.label[:24] for c in menu.section_rows("news")]
    assert all(r["id"].startswith("c:") for r in rows)


def test_commands_are_not_lists():
    assert owner_commands.menu_list("آية اليوم") is None
