"""The owner's command menu: one list for both bots, exact matching only."""

import unittest

from studio_core import commands as menu


class MenuTests(unittest.TestCase):
    def test_numbers_are_one_to_n_in_section_order(self):
        self.assertEqual([c.number for c in menu.COMMANDS], list(range(1, len(menu.COMMANDS) + 1)))
        order = [section for section, _, _ in menu.SECTIONS]
        seen = [c.section for c in menu.COMMANDS]
        self.assertEqual(sorted(set(seen), key=order.index), order)
        self.assertEqual(seen, sorted(seen, key=order.index), "guard:section-order")

    def test_every_list_fits_whatsapp(self):
        self.assertLessEqual(len(menu.whatsapp_list()["sections"][0]["rows"]), 10)
        for section, _, _ in menu.SECTIONS:
            rows = menu.whatsapp_list(section)["sections"][0]["rows"]
            self.assertLessEqual(len(rows), 10, "guard:whatsapp-rows")
            self.assertTrue(all(len(r["title"]) <= 24 for r in rows))

    def test_number_name_emoji_and_spelling_variants_match(self):
        for text in ("7", "٧", "هل تعلم", "💡 هل تعلم", "  هل تعلم  "):
            parsed = menu.parse(text)
            self.assertEqual((parsed["kind"], parsed["command"].key), ("command", "facts"))
        self.assertEqual(menu.parse("اذكار الصباح")["command"].key, "adhkar_am")
        self.assertEqual(menu.parse("أذكار المساء")["command"].key, "adhkar_pm")
        self.assertEqual(menu.parse("ألغِ")["command"].key, "cancel")

    def test_ordinary_sentences_never_run_a_command(self):
        # The old bot ran commands found inside sentences ("لا توقف شي" paused the schedule).
        for text in (
            "لا توقف شي",
            "وش النهاية؟",
            "شكرا على الشغل",
            "وقفته؟",
            "هل تعلم شي؟",
            "37",
            "",
        ):
            self.assertEqual(menu.parse(text)["kind"], "menu", "guard:exact-match")

    def test_sections_open_and_menu_words_show_the_menu(self):
        self.assertEqual(menu.parse("📰 أخبار"), {"kind": "section", "section": "news"})
        self.assertEqual(menu.parse("أخبار"), {"kind": "section", "section": "news"})
        for text in ("0", "/start", "القائمة", "مساعدة", "help"):
            self.assertEqual(menu.parse(text)["kind"], "menu")
        self.assertEqual(menu.parse("يومك في دقيقة")["command"].key, "brief")

    def test_confirmations_and_new_facts(self):
        self.assertTrue(menu.BY_KEY["send"].confirm and menu.BY_KEY["halt"].confirm)
        self.assertFalse(menu.BY_KEY["preview"].confirm)
        self.assertEqual(menu.parse("نعم"), {"kind": "yes"})
        self.assertEqual(
            menu.parse("أضف هل تعلم: الشمس نجم"), {"kind": "add_fact", "text": "الشمس نجم"}
        )
        self.assertEqual(menu.parse("أضف هل تعلم:")["kind"], "menu")

    def test_no_spelling_reaches_two_commands(self):
        forms = {}
        for command in menu.COMMANDS:
            for form in (str(command.number), command.label, *command.aliases):
                forms.setdefault(menu.normalize(form), set()).add(command.key)
        self.assertTrue(all(len(keys) == 1 for keys in forms.values()), "guard:unambiguous")

    def test_menu_and_section_text(self):
        text = menu.menu_text()
        for _, title, _ in menu.SECTIONS:
            self.assertIn(title, text)
        self.assertIn("(21)", text)
        self.assertIn("28. ✅ أرسل (يحتاج نعم)", menu.section_text("control"))


if __name__ == "__main__":
    unittest.main()
