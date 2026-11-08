"""Telegram shows the same sections as WhatsApp (owner decision 8 Nov 2026)."""

from studio_core import commands as menu


def labels(keyboard):
    return [b["text"] for row in keyboard["keyboard"] for b in row]


def test_first_keyboard_has_every_whatsapp_section():
    top = labels(menu.telegram_keyboard())
    whatsapp = [r["title"] for r in menu.whatsapp_list()["sections"][0]["rows"]]
    assert len(top) == len(whatsapp) == len(menu.SECTIONS)
    for label in top:
        assert menu.parse(label)["kind"] in {"section", "command", "add_fact", "menu"}
        assert menu.parse(label)["kind"] != "menu" or label == menu.BY_KEY["menu"].label


def test_every_command_is_reachable_through_its_section():
    reached = set()
    for section, _, _ in menu.SECTIONS:
        for label in labels(menu.telegram_keyboard(section)):
            parsed = menu.parse(label)
            if parsed["kind"] == "command":
                reached.add(parsed["command"].key)
            elif label == menu.BY_KEY["add_fact"].label:
                reached.add("add_fact")
            else:
                assert parsed["kind"] == "menu"  # the back button
    assert reached == {c.key for c in menu.COMMANDS} - {"menu"}


def test_section_keyboard_ends_with_back_to_menu():
    assert labels(menu.telegram_keyboard("faith"))[-1] == menu.BY_KEY["menu"].label
