import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from snap_bot.did_you_know import (
    CATALOG,
    WINDOW_SECONDS,
    FactsExhausted,
    choose_fact,
    load_facts,
    make_content,
    validate_content,
    validate_fact,
)
from studio_core.config import Config
from studio_core.jobs import LeaseLost, Repository

ROOT = Path(__file__).parents[3]


class CuratedFactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 10, 3, 12, tzinfo=UTC).timestamp()
        self.config = replace(
            Config.load(ROOT / "config/snap/weather.toml"),
            data_root=self.root / "data",
            max_running=2,
            rate_limit=100,
            max_active_per_actor=100,
        )
        self.repo = Repository(self.config, clock=lambda: self.now)
        self.facts = load_facts(today=date(2026, 10, 3))

    def claim(self):
        job, _ = self.repo.enqueue(
            str(time.time_ns()), "synthetic", "did_you_know", "NORMAL", "input", self.now + 600
        )
        return self.repo.claim(job_id=job["id"])

    def choose(self, claim, catalog=CATALOG):
        return choose_fact(
            self.repo, claim["id"], claim["lease_token"], claim["attempt"], catalog=catalog
        )[0]

    def one_catalog(self):
        path = self.root / "one.json"
        path.write_text(
            json.dumps(
                {"schema": 1, "language": "ar", "sacred": False, "facts": self.facts[:1]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def ack(self, claim, at):
        with self.repo.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS telegram_outbox(video_sha TEXT PRIMARY KEY,job_id TEXT,state TEXT,message_id INTEGER,updated_at REAL)"
            )
            db.execute(
                "INSERT INTO telegram_outbox VALUES(?,?,?,?,?)",
                (claim["id"], claim["id"], "PUBLISHED", 7, at),
            )
            db.execute(
                "INSERT INTO deliveries VALUES(?,?,?)", (claim["id"], claim["id"], "PUBLISHED")
            )

    def test_catalog_has_30_short_reviewed_facts(self):
        self.assertEqual(len(self.facts), 30)
        self.assertTrue(
            all(len(f["text"]) <= 120 and f["verified_at"] == "2026-10-03" for f in self.facts)
        )
        self.assertEqual(
            next(f for f in self.facts if f["id"] == "dyk-21")["text"],
            "الربع الخالي من أكبر البحار الرملية المتصلة في العالم.",
        )

    def test_missing_source_or_date_is_rejected(self):
        for key in ("source", "source_url", "verified_at"):
            bad = dict(self.facts[0])
            bad.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_fact(bad, date(2026, 10, 3))

    def test_invalid_source_future_date_and_long_text_rejected(self):
        for changes in (
            {"source_url": "http://science.nasa.gov/x"},
            {"source_url": "https://example.com/x"},
            {"verified_at": "2027-01-01"},
            {"text": "ا" * 121},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_fact({**self.facts[0], **changes}, date(2026, 10, 3))

    def test_two_jobs_cannot_repeat_reserved_fact(self):
        first = self.claim()
        second = self.claim()
        self.assertNotEqual(self.choose(first)["fact_id"], self.choose(second)["fact_id"])

    def test_two_concurrent_workers_reserve_different_facts(self):
        claims = [self.claim(), self.claim()]
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def select(claim):
            try:
                barrier.wait(timeout=5)
                results.append(self.choose(claim)["fact_id"])
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=select, args=(claim,)) for claim in claims]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(set(results)), 2)

    def test_competing_workers_one_fact_only_one_reservation(self):
        path = self.one_catalog()
        claims = [self.claim(), self.claim()]
        barrier = threading.Barrier(2)
        chosen = []
        exhausted = []
        unexpected = []

        def reserve(claim):
            try:
                barrier.wait(timeout=5)
                chosen.append(self.choose(claim, path)["fact_id"])
            except FactsExhausted:
                exhausted.append(claim["id"])
            except BaseException as error:
                unexpected.append(type(error).__name__)

        threads = [threading.Thread(target=reserve, args=(claim,)) for claim in claims]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(unexpected, [])
        self.assertEqual(
            (len(chosen), len(exhausted)), (1, 1), msg="guard:competing-fact-reservation"
        )
        with self.repo.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM fact_reservations").fetchone()[0],
                1,
                msg="guard:competing-fact-reservation",
            )

    def test_same_job_and_reopened_repository_keep_identical_content(self):
        claim = self.claim()
        content = self.choose(claim)
        self.repo = Repository(self.config, clock=lambda: self.now + 1)
        self.assertEqual(self.choose(claim), content)

    def test_30_day_boundary_and_exhaustion(self):
        path = self.one_catalog()
        first = self.claim()
        self.choose(first, path)
        self.ack(first, self.now)
        self.repo.transition(first["id"], first["lease_token"], "FAILED", {"code": "SYNTHETIC"})
        self.now += WINDOW_SECONDS - 1
        second = self.claim()
        with self.assertRaises(FactsExhausted):
            self.choose(second, path)
        self.repo.transition(second["id"], second["lease_token"], "FAILED", {"code": "SYNTHETIC"})
        self.now += 1
        third = self.claim()
        self.assertEqual(self.choose(third, path)["fact_id"], self.facts[0]["id"])

    def test_failed_without_delivery_releases_reservation(self):
        path = self.one_catalog()
        first = self.claim()
        content = self.choose(first, path)
        self.repo.transition(first["id"], first["lease_token"], "FAILED", {"code": "RENDER_FAILED"})
        try:
            chosen = self.choose(self.claim(), path)
        except FactsExhausted:
            self.fail("Terminal failure without a delivery must release its fact")
        self.assertEqual(chosen["fact_id"], content["fact_id"])

    def test_unknown_delivery_stays_reserved_after_terminal_failure(self):
        path = self.one_catalog()
        first = self.claim()
        self.choose(first, path)
        with self.repo.transaction() as db:
            db.execute(
                "INSERT INTO deliveries VALUES(?,?,?)",
                ("synthetic-effect", first["id"], "DELIVERY_UNKNOWN"),
            )
        self.repo.transition(
            first["id"], first["lease_token"], "FAILED", {"code": "DELIVERY_UNKNOWN"}
        )
        with self.assertRaises(FactsExhausted, msg="guard:delivery-evidence-retains-reservation"):
            self.choose(self.claim(), path)

    def test_window_starts_from_ack_not_earlier_reservation(self):
        path = self.one_catalog()
        first = self.claim()
        self.choose(first, path)
        reserved = self.now
        self.ack(first, self.now + 60)
        self.repo.transition(
            first["id"], first["lease_token"], "FAILED", {"code": "LOCAL_FAILURE_AFTER_ACK"}
        )
        self.now = reserved + WINDOW_SECONDS + 1
        with self.assertRaises(FactsExhausted, msg="guard:window-starts-at-ack"):
            self.choose(self.claim(), path)

    def test_categories_rotate_and_do_not_start_with_five_space_facts(self):
        categories = []
        for _ in range(6):
            claim = self.claim()
            categories.append(self.choose(claim)["category"])
            self.ack(claim, self.now)
            self.repo.transition(claim["id"], claim["lease_token"], "FAILED", {"code": "SYNTHETIC"})
        self.assertEqual(
            categories, ["animals", "body", "geography", "technology", "space", "animals"]
        )

    def test_lost_owner_cannot_reserve_or_record_event(self):
        first = self.claim()
        self.now += self.config.lease_seconds + 1
        self.repo.claim(job_id=first["id"])
        before = self.repo.timeline(first["id"])
        with self.assertRaises(LeaseLost):
            self.choose(first)
        self.assertEqual(self.repo.timeline(first["id"]), before)
        with self.repo.connect() as db:
            self.assertIsNone(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE name='fact_reservations'"
                ).fetchone()
            )

    def test_content_is_bound_to_curated_fact_source_and_sacred_flag(self):
        content = make_content(self.facts[0], self.now)
        validate_content(content)
        from snap_bot.weather import content_hash

        for changes in (
            {"body": "حقيقة غير معتمدة"},
            {"source_url": "https://science.nasa.gov/other/"},
            {"sacred": True},
            {"type": "unknown"},
            {"footer": "نص آخر"},
        ):
            bad = {**content, **changes}
            bad["content_hash"] = content_hash(bad)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_content(bad)


if __name__ == "__main__":
    unittest.main()
