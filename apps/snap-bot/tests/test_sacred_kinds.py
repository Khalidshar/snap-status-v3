"""Hadith and dua through the fact pipeline: verbatim text, never proofread or stripped."""

import hashlib
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from snap_bot import did_you_know as dyk
from snap_bot import faith, sacred_kinds
from snap_bot.arabic_agent import check_text, inspect_content
from snap_bot.content_agents import CHECKS, AgentRejected, proofread, run_check
from snap_bot.fact_layout import (
    HADITH_BODY,
    PROPHET_LINE,
    SACRED_BODY,
    fact_labels,
    sacred_body_box,
    sacred_labels,
)
from studio_core.config import Config
from studio_core.jobs import Repository

ROOT = Path(__file__).parents[3]


def contents(kind):
    now = time.time()
    return [dyk.make_content(fact, now, kind) for fact in dyk.load_kind(kind)]


class SacredKindTests(unittest.TestCase):
    def test_kinds_are_registered_as_sacred_with_plain_titles(self):
        self.assertEqual(set(sacred_kinds.TITLES), {"hadith", "dua"})
        self.assertLessEqual(set(sacred_kinds.TITLES), dyk.SACRED_KINDS)
        for kind in ("tech", "night", dyk.DEFAULT_KIND):
            self.assertNotIn(kind, dyk.SACRED_KINDS, "guard:only-faith-is-sacred")

    def test_every_item_is_the_approved_text_byte_for_byte(self):
        for kind in sacred_kinds.TITLES:
            items = {item["id"]: item for item in faith.load(kind)}
            for content in contents(kind):
                with self.subTest(kind=kind, fact=content["fact_id"]):
                    item = items[content["fact_id"]]
                    self.assertIs(content["sacred"], True)
                    self.assertEqual(content["body"], item["text"], "guard:verbatim")
                    self.assertEqual(
                        hashlib.sha256(content["body"].encode()).hexdigest(), item["sha256"]
                    )
                    self.assertEqual(dyk.validate_content(content)["id"], content["fact_id"])
                    report = inspect_content(content)
                    self.assertEqual(report["checks"], ["verbatim_sha256"])
                    self.assertEqual(report["sha256"], item["sha256"])

    def test_one_changed_letter_is_refused_everywhere(self):
        content = contents("hadith")[0]
        changed = {**content, "body": content["body"].replace("َ", "", 1)}
        with self.assertRaises(ValueError):
            dyk.validate_content(changed)
        with self.assertRaisesRegex(ValueError, "ARABIC_SACRED_CHANGED"):
            inspect_content(changed)

    def test_the_sacred_flag_cannot_be_forged_either_way(self):
        sacred = contents("dua")[0]
        plain = contents("night")[0]
        for bad in ({**sacred, "sacred": False}, {**plain, "sacred": True}):
            with self.subTest(kind=bad["kind"]), self.assertRaises(ValueError):
                dyk.validate_content(bad)
        with self.assertRaises(ValueError):
            inspect_content({**plain, "sacred": True})

    def test_writing_rules_do_not_judge_faith_text(self):
        # dua-04 is longer than any fact card line budget; the writing rules would refuse it.
        long_dua = next(c for c in contents("dua") if c["fact_id"] == "dua-04")
        with self.assertRaises(ValueError):
            check_text(long_dua["body"])
        self.assertEqual(inspect_content(long_dua)["decision"], "PASS")
        with self.assertRaises(AgentRejected):
            proofread("نص  فيه مسافتان")

    def test_the_card_never_wraps_or_strips_the_body(self):
        content = contents("hadith")[0]
        with self.assertRaises(ValueError):
            fact_labels(content)
        labels = [label[0] for label in sacred_labels(content)]
        self.assertIn("حديث شريف", labels)
        self.assertEqual(len(labels), 6, "guard:no-body-lines")
        self.assertFalse(any(content["body"][:10] in label for label in labels))
        with self.assertRaises(ValueError):
            sacred_labels(contents("tech")[0])

    def test_every_hadith_is_attributed_to_the_prophet(self):
        for content in contents("hadith"):
            with self.subTest(fact=content["fact_id"]):
                labels = [label[0] for label in sacred_labels(content)]
                self.assertEqual(labels[3], PROPHET_LINE, "guard:prophet-line")
                self.assertIn("ﷺ", labels[3])
                self.assertEqual(sacred_body_box(content), HADITH_BODY)
        dua = contents("dua")[0]
        self.assertNotIn(PROPHET_LINE, [label[0] for label in sacred_labels(dua)])
        self.assertEqual(sacred_body_box(dua), SACRED_BODY)
        # The line sits between the title and the verbatim body, never inside the body.
        self.assertLess(575 + 64, HADITH_BODY[1])
        self.assertNotIn(PROPHET_LINE, contents("hadith")[0]["body"])

    def test_a_rendered_hadith_seeds_the_next_render_history(self):
        # Every server render seeds the ledger history; a faith ID there must not block it.
        import sqlite3

        content = contents("hadith")[0]
        entry = {
            "fact_id": content["fact_id"],
            "body_hash": hashlib.sha256(content["body"].encode()).hexdigest(),
            "category": "hadith",
            "at": 1.0,
            "until": 2.0,
            "kind": "hadith",
        }
        self.assertEqual(dyk.history_entry(entry)[0], "bukhari_1")
        db = sqlite3.connect(":memory:")
        dyk.seed_history(db, [entry, {**entry, "fact_id": "muslim_2564"}])
        for bad in ("Bukhari_1", "x", "dyk 07", "a" * 41):
            with self.subTest(fact_id=bad), self.assertRaises(ValueError):
                dyk.history_entry({**entry, "fact_id": bad})


class SacredPipelineTests(unittest.TestCase):
    def test_reserved_hadith_passes_every_check_agent_unchanged(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = replace(
            Config.load(ROOT / "config/snap/did_you_know.toml"),
            data_root=Path(tmp.name) / "data",
            max_running=20,
            rate_limit=100,
            max_active_per_actor=100,
        )
        repo = Repository(config, clock=time.time)
        seen = set()
        for _ in range(len(faith.load("hadith"))):
            job, _ = repo.enqueue(
                str(time.time_ns()),
                "synthetic",
                "did_you_know",
                "NORMAL",
                "input",
                time.time() + 600,
            )
            claim = repo.claim(job_id=job["id"])
            ids = claim["id"], claim["lease_token"], claim["attempt"]
            content = dyk.choose_fact(repo, *ids, kind="hadith")[0]
            for role in CHECKS:
                self.assertEqual(run_check(role, repo, ids[0], ids[1], ids[2], content), content)
            seen.add(content["fact_id"])
        self.assertEqual(len(seen), len(faith.load("hadith")), "guard:no-repeat")
        job, _ = repo.enqueue(
            str(time.time_ns()), "synthetic", "did_you_know", "NORMAL", "input", time.time() + 600
        )
        claim = repo.claim(job_id=job["id"])
        with self.assertRaises(dyk.FactsExhausted):
            dyk.choose_fact(
                repo, claim["id"], claim["lease_token"], claim["attempt"], kind="hadith"
            )


if __name__ == "__main__":
    unittest.main()
