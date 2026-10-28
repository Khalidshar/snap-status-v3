"""More than one reviewed catalog through the same fact pipeline, without repeats."""

import hashlib
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from snap_bot import did_you_know as dyk
from snap_bot.content_agents import check_memory, check_rotation
from snap_bot.fact_layout import fact_labels
from studio_core.config import Config
from studio_core.jobs import Repository

ROOT = Path(__file__).parents[3]
SAMPLE = [
    {
        "id": f"sample-{n:02d}",
        "category": "sample_a" if n % 2 else "sample_b",
        "text": f"سطر تجريبي رقم {n} للفحص.",
        "source": "original",
        "source_url": "",
        "verified_at": "2026-10-28",
    }
    for n in range(1, 5)
]


class ContentKindTests(unittest.TestCase):
    def setUp(self):
        dyk.register_kind("sample", "📌 تجربة", lambda today: [dict(f) for f in SAMPLE])
        self.addCleanup(dyk.KINDS.pop, "sample")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 10, 28, 12, tzinfo=UTC).timestamp()
        self.config = replace(
            Config.load(ROOT / "config/snap/weather.toml"),
            data_root=Path(self.tmp.name) / "data",
            max_running=20,
            rate_limit=100,
            max_active_per_actor=100,
        )
        self.repo = Repository(self.config, clock=lambda: self.now)

    def claim(self):
        job, _ = self.repo.enqueue(
            str(time.time_ns()), "synthetic", "did_you_know", "NORMAL", "input", self.now + 600
        )
        return self.repo.claim(job_id=job["id"])

    def choose(self, kind):
        claim = self.claim()
        content = dyk.choose_fact(
            self.repo, claim["id"], claim["lease_token"], claim["attempt"], kind=kind
        )[0]
        return claim, content

    def test_original_kind_is_unchanged(self):
        fact = dyk.load_facts()[0]
        content = dyk.make_content(fact, self.now)
        self.assertNotIn("kind", content, "guard:original-hash-stable")
        self.assertEqual(content["title"], "هل تعلم؟")
        self.assertEqual(dyk.content_kind(content), dyk.DEFAULT_KIND)

    def test_second_kind_reserves_its_own_catalog_with_its_title(self):
        _, content = self.choose("sample")
        self.assertEqual(content["kind"], "sample")
        self.assertEqual(content["title"], "📌 تجربة")
        self.assertEqual(content["attribution"], "روّق مع خالد")
        self.assertTrue(content["fact_id"].startswith("sample-"))
        self.assertEqual(dyk.validate_content(content)["id"], content["fact_id"])
        labels = [label[0] for label in fact_labels(content)]
        self.assertIn("📌 تجربة", labels, "guard:title-from-kind")

    def test_rotation_is_per_kind_and_repeats_are_global(self):
        seen = []
        for _ in range(4):
            claim, content = self.choose("sample")
            seen.append(content["fact_id"])
            with self.repo.connect() as db:
                check_memory(db, claim["id"], content)
                check_rotation(db, claim["id"], content)
        self.assertEqual(len(set(seen)), 4, "guard:no-repeat-within-kind")
        _, fact = self.choose(dyk.DEFAULT_KIND)
        self.assertEqual(fact["category"], dyk.CATEGORIES[0], "guard:rotation-per-kind")
        claim = self.claim()
        with self.assertRaises(dyk.FactsExhausted):
            dyk.choose_fact(
                self.repo, claim["id"], claim["lease_token"], claim["attempt"], kind="sample"
            )

    def test_unknown_or_forged_kind_is_refused(self):
        _, content = self.choose("sample")
        for bad in ({**content, "kind": "nope"}, {**content, "kind": dyk.DEFAULT_KIND}):
            with self.subTest(kind=bad["kind"]), self.assertRaises(ValueError):
                dyk.validate_content(bad)
        with self.assertRaises(ValueError):
            dyk.register_kind("sample", "مكرر", lambda today: [])

    def test_history_carries_the_kind(self):
        body = hashlib.sha256(SAMPLE[0]["text"].encode()).hexdigest()
        entry = {
            "fact_id": "sample-01",
            "body_hash": body,
            "category": "sample_a",
            "at": self.now - 60,
            "until": self.now + 600,
            "kind": "sample",
        }
        with self.repo.transaction() as db:
            dyk.seed_history(db, [entry])
        _, content = self.choose("sample")
        self.assertNotEqual(content["fact_id"], "sample-01", "guard:history-blocks-kind")
        self.assertEqual(content["category"], "sample_b", "guard:history-rotates-kind")
        for bad in ({**entry, "category": "animals"}, {**entry, "kind": "nope"}):
            with self.subTest(bad=bad), self.repo.transaction() as db:
                with self.assertRaises(ValueError):
                    dyk.seed_history(db, [bad])


if __name__ == "__main__":
    unittest.main()
