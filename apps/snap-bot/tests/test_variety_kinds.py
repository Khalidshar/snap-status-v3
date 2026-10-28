"""The four variety catalogs as fact kinds: every item renders through the same checks."""

import subprocess
import sys
import time
import unittest

from snap_bot import did_you_know as dyk
from snap_bot import variety_kinds
from snap_bot.arabic_agent import inspect_content
from snap_bot.fact_layout import fact_labels


class VarietyKindTests(unittest.TestCase):
    def test_the_four_kinds_are_registered_with_plain_titles(self):
        for kind, title in variety_kinds.TITLES.items():
            with self.subTest(kind=kind):
                self.assertEqual(dyk.KINDS[kind][0], title)
                self.assertTrue(all("؀" <= c <= "ۿ" or c == " " for c in title))
        self.assertNotIn("riddle", dyk.KINDS, "guard:riddle-waits-for-its-card")

    def test_every_item_passes_the_fact_checks_and_fits_the_card(self):
        now = time.time()
        for kind in variety_kinds.TITLES:
            for fact in dyk.load_kind(kind):
                with self.subTest(kind=kind, fact=fact["id"]):
                    content = dyk.make_content(fact, now, kind)
                    self.assertEqual(dyk.validate_content(content)["id"], fact["id"])
                    inspect_content(content)
                    labels = [label[0] for label in fact_labels(content)]
                    self.assertIn(dyk.KINDS[kind][0], labels)
                    self.assertLessEqual(len(content["attribution"]), 24, "guard:one-line")

    def test_every_module_imports_first_without_a_cycle(self):
        # The renderer starts from content_agents; a cycle there kills the sandbox.
        for module in ("content_agents", "catalogs", "render", "did_you_know", "fact_layout"):
            with self.subTest(module=module):
                result = subprocess.run(  # fixed interpreter and module names
                    [sys.executable, "-c", "import snap_bot." + module],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stderr[-500:])

    def test_original_items_credit_the_channel(self):
        fact = dyk.load_kind("night")[0]
        self.assertEqual(dyk.make_content(fact, 0, "night")["attribution"], "روّق مع خالد")


if __name__ == "__main__":
    unittest.main()
