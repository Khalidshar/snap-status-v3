import gc
import hashlib
import importlib
import json
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from snap_bot.agent_contract_map import LOCAL_TOOLS
from snap_bot.content_agents import AgentRejected
from snap_bot.did_you_know import CATALOG, CATEGORIES, load_facts, make_content, rotation_order
from snap_bot.editorial_agents import (
    PROPOSAL_KEYS,
    catalog_facts,
    fix,
    propose,
    review,
    skeleton,
    write,
)
from snap_bot.oversight_agents import analyse, plan
from studio_core.config import Config
from studio_core.jobs import Repository

ROOT = Path(__file__).parents[3]
TODAY = date(2026, 10, 13)


def proposal(**changes):
    return {
        "category": "space",
        "text": "يدور كوكب نبتون حول الشمس مرة كل 165 سنة أرضية تقريبًا.",
        "source": "NASA",
        "source_url": "https://science.nasa.gov/neptune/facts/",
        "verified_at": "2026-10-12",
        **changes,
    }


class EditorialAgentTests(unittest.TestCase):
    def setUp(self):
        self.facts = catalog_facts()

    def test_every_local_tool_role_resolves_to_code(self):
        for role, target in LOCAL_TOOLS.items():
            module, name = target.split(".")
            with self.subTest(role=role):
                self.assertTrue(
                    callable(getattr(importlib.import_module("snap_bot." + module), name))
                )

    def test_new_fact_is_ready_for_owner_and_catalog_untouched(self):
        before = CATALOG.read_bytes()
        result = propose(proposal(), today=TODAY)
        self.assertEqual(result["status"], "READY_FOR_OWNER")
        self.assertFalse(result["catalog_written"])
        self.assertEqual(result["draft"]["id"], "dyk-31")
        self.assertEqual(CATALOG.read_bytes(), before, msg="guard:no-catalog-write")

    def test_every_catalog_fact_passes_review_against_the_others(self):
        for fact in self.facts:
            with self.subTest(fact=fact["id"]):
                self.assertEqual(
                    review(fact, [f for f in self.facts if f is not fact], today=TODAY)["problems"],
                    [],
                )

    def test_writer_refuses_incomplete_or_unknown_category(self):
        with self.assertRaisesRegex(AgentRejected, "WRITER_PROPOSAL_INCOMPLETE"):
            write({"text": "x"}, self.facts)
        with self.assertRaisesRegex(
            AgentRejected, "WRITER_CATEGORY_UNKNOWN", msg="guard:writer-category"
        ):
            write(proposal(category="sports"), self.facts)
        self.assertEqual(propose(proposal(category="sports"), today=TODAY)["stage"], "writer")

    def test_writer_takes_the_first_free_identity(self):
        facts = [f for f in self.facts if f["id"] != "dyk-07"]
        self.assertEqual(write(proposal(), facts)["id"], "dyk-07", msg="guard:writer-free-id")

    def test_reviewer_rejects_duplicates_and_close_rewordings(self):
        same = self.facts[0]
        result = propose(proposal(text=same["text"]), today=TODAY)
        self.assertIn(
            "DUPLICATE_OF_" + same["id"], result["problems"], msg="guard:reviewer-duplicate"
        )
        reworded = "يستغرق دوران الزهرة حول نفسه نحو 243 يومًا، بينما يدور حول الشمس في 225 يومًا."
        self.assertIn(
            "SIMILAR_TO_dyk-01",
            propose(proposal(text=reworded), today=TODAY)["problems"],
            msg="guard:reviewer-similar",
        )

    def test_reviewer_rejects_sources_latin_and_layout(self):
        for changes, code in (
            ({"source_url": "https://example.com/x"}, "SOURCE_UNAPPROVED_FACT_SOURCE"),
            ({"verified_at": "2027-01-01"}, "SOURCE_FUTURE_VERIFICATION_DATE"),
            ({"text": "هذه حقيقة فيها كلمة planet غير مترجمة."}, "ARABIC_LATIN_UNAPPROVED"),
            (
                {
                    "text": "يدور كوكب نبتون البعيد جدًا حول الشمس مرة واحدة كل مئة وخمس وستين سنة أرضية كاملة تقريبًا في مدار واسع."
                },
                "LAYOUT_TOO_LONG",
            ),
        ):
            with self.subTest(code=code):
                result = propose(proposal(**changes), today=TODAY)
                self.assertEqual(result["status"], "REJECTED")
                self.assertIn(code, result["problems"], msg="guard:reviewer-" + code)

    def test_fixer_corrects_spacing_and_punctuation_only(self):
        messy = "يدور كوكب نبتون  حول الشمس مرة كل 165 سنة أرضيـة ، تقريبًا.."
        result = propose(proposal(text=messy), today=TODAY)
        self.assertTrue(result["fixed"])
        self.assertEqual(result["status"], "READY_FOR_OWNER", msg="guard:fixer-round")
        self.assertEqual(
            result["draft"]["text"], "يدور كوكب نبتون حول الشمس مرة كل 165 سنة أرضية، تقريبًا."
        )
        self.assertEqual(skeleton(result["draft"]["text"]), skeleton(messy))

    def test_fixer_converts_latin_punctuation_but_keeps_grouped_numbers(self):
        draft = write(proposal(text="يبلغ عدد سكان المدينة 1,250,000 نسمة, تقريبًا?"), self.facts)
        fixed = fix(draft, review(draft, self.facts, today=TODAY))
        self.assertEqual(fixed["text"], "يبلغ عدد سكان المدينة 1,250,000 نسمة، تقريبًا؟")

    def test_fixer_refuses_problems_that_need_new_words(self):
        draft = write(proposal(text="هذه حقيقة فيها كلمة planet  غير مترجمة."), self.facts)
        self.assertIsNone(
            fix(draft, review(draft, self.facts, today=TODAY)), msg="guard:fixer-scope"
        )

    def test_fixer_never_changes_words(self):
        from unittest.mock import patch

        draft = write(proposal(text="يدور كوكب نبتون  حول الشمس."), self.facts)
        with patch(
            "snap_bot.editorial_agents.re.sub",
            side_effect=lambda p, r, t, *a, **k: t.replace("نبتون", "زحل"),
        ):
            with self.assertRaisesRegex(
                AgentRejected, "FIXER_CHANGED_WORDS", msg="guard:fixer-words"
            ):
                fix(draft, review(draft, self.facts, today=TODAY))


class OversightTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(gc.collect)
        self.now = time.time()
        self.count = 0
        config = replace(
            Config.load(ROOT / "config/snap/weather.toml"),
            data_root=Path(temp.name) / "data",
            rate_limit=100,
        )
        self.repo = Repository(config, clock=lambda: self.now)

    def job(self, state="FAILED", code="VERTICAL_SLICE_FAILED", category=None):
        self.count += 1
        job, _ = self.repo.enqueue(
            f"job-{self.count}",
            f"actor-{self.count}",
            "did_you_know",
            "NORMAL",
            "input",
            self.now + 900,
        )
        claim = self.repo.claim(job_id=job["id"])
        if category:
            fact = next(f for f in load_facts() if f["category"] == category)
            content = make_content(fact, self.now)
            with self.repo.transaction() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS fact_reservations(job_id TEXT PRIMARY KEY REFERENCES jobs(id),fact_id TEXT NOT NULL,body_hash TEXT NOT NULL,reserved_at REAL NOT NULL,content_json TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT INTO fact_reservations VALUES(?,?,?,?,?)",
                    (
                        job["id"],
                        fact["id"],
                        hashlib.sha256(fact["text"].encode()).hexdigest(),
                        self.now,
                        json.dumps(content, ensure_ascii=False),
                    ),
                )
        if state == "FAILED":
            self.repo.transition(job["id"], claim["lease_token"], "FAILED", {"code": code})
        elif state == "VALIDATING":
            for s in (
                "RESEARCHING",
                "PROCESSING",
                "DOWNLOADING",
                "DESIGNING",
                "RENDERING",
                "VALIDATING",
            ):
                self.repo.transition(job["id"], claim["lease_token"], s)
            with self.repo.transaction() as db:
                db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (self.now - 1, job["id"]))
        return job["id"]

    def snapshot(self):
        with self.repo.connect() as db:
            return {
                t: sorted(tuple(r) for r in db.execute("SELECT * FROM " + t))
                for (t,) in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

    def test_analyst_counts_outcomes_and_causes(self):
        self.job(code="AGENT_CONTRACT_FAILED")
        self.job(code="AGENT_CONTRACT_FAILED")
        self.job(state="VALIDATING")
        report = analyse(self.repo)
        self.assertEqual(report["did_you_know_success_rate"], 0.333, msg="guard:analyst-rate")
        self.assertEqual(
            report["failure_causes"], {"AGENT_CONTRACT_FAILED": 2}, msg="guard:analyst-causes"
        )
        self.assertEqual(sum(report["fact_stock"].values()), len(load_facts()))
        self.assertTrue(report["summary_ar"])

    def test_analyst_and_manager_change_nothing(self):
        self.job()
        before = self.snapshot()
        analyse(self.repo)
        plan(self.repo)
        self.assertEqual(before, self.snapshot(), msg="guard:read-only")

    def test_manager_picks_what_choose_fact_would(self):
        result = plan(self.repo)
        self.assertEqual(
            (result["decision"], result["next_category"]), ("GO", rotation_order(None)[0])
        )
        first = next(f["id"] for f in load_facts() if f["category"] == rotation_order(None)[0])
        self.assertEqual(result["next_fact_id"], first)
        self.job(state="VALIDATING", category="animals")
        self.assertEqual(
            plan(self.repo)["next_category"],
            rotation_order("animals")[0],
            msg="guard:manager-rotation",
        )
        self.assertFalse(result["sends"])
        self.assertFalse(result["reserves"])
        self.assertFalse(result["claims"])

    def test_manager_holds_after_repeated_failures(self):
        for _ in range(3):
            self.job()
        self.assertIn("REPEATED_FAILURES", plan(self.repo)["reasons"], msg="guard:manager-failures")
        self.job(state="VALIDATING")
        self.assertEqual(plan(self.repo)["decision"], "GO")

    def test_manager_holds_while_a_job_is_active(self):
        job, _ = self.repo.enqueue(
            "active", "actor-active", "did_you_know", "NORMAL", "input", self.now + 900
        )
        self.assertEqual(plan(self.repo)["reasons"], ["QUEUE_BUSY"], msg="guard:manager-busy")

    def test_manager_holds_when_the_catalog_is_exhausted(self):
        catalog = Path(tempfile.mkdtemp()) / "catalog.json"
        self.addCleanup(lambda: catalog.unlink())
        data = json.loads(CATALOG.read_text(encoding="utf-8"))
        data["facts"] = [next(f for f in data["facts"] if f["category"] == "animals")]
        catalog.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.job(state="VALIDATING", category="animals")
        result = plan(self.repo, catalog=catalog)
        self.assertEqual(
            (result["decision"], result["reasons"]),
            ("HOLD", ["CATALOG_EXHAUSTED"]),
            msg="guard:manager-exhausted",
        )


if __name__ == "__main__":
    unittest.main()
