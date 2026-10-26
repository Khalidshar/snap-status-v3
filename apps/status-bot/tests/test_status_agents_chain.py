"""Status's eighteen agents in Snap's order: the checks and the six local tools."""

import copy
import hashlib
import json
import sqlite3
import struct
import unittest

import test_status_agent_runs as runs

from status_bot import agents, oversight
from status_bot.pipeline import record_validation
from studio_core.jobs import Repository
from studio_core.observability.agent_inventory import ROLE_IDS
from studio_core.observability.agent_runs import AgentRejected


class RoleMapTests(unittest.TestCase):
    def test_eighteen_roles_in_snaps_order(self):
        # Snap's order (snap_bot.agent_contract_map.BOUND + final); apps never import each other.
        snap_chain = (
            "fact_source", "arabic", "memory", "rotation", "proofreading", "translation",
            "design", "voice", "media_verification", "judge", "quality", "final",
        )  # fmt: skip
        snap_tools = {"manager", "analyst", "writer", "reviewer", "fixer", "content_repair"}
        self.assertEqual(sorted(agents.ROLES), sorted(ROLE_IDS), "guard:status-eighteen")
        self.assertEqual(agents.CHAIN, snap_chain, "guard:snap-order")
        self.assertEqual(set(agents.LOCAL_TOOLS), snap_tools)
        for i, role in enumerate(agents.CHAIN):
            self.assertEqual(agents.contract(role).dependencies, agents.CHAIN[i - 1 : i])

    def test_unknown_role_refused(self):
        with self.assertRaises(ValueError):
            agents.contract("manager")


class CheckTests(unittest.TestCase):
    content: dict = {  # noqa: RUF012  # read-only fixture
        "title": "طقس الرياض",
        "body": "الحرارة 25.0 درجة مئوية\nالرياح 10.0 كم في الساعة",
        "source": "open-meteo",
    }

    def test_rotation_wants_the_scheduled_city(self):
        agents.check_rotation(self.content, "الرياض")
        with self.assertRaises(agents.CheckRejected):
            agents.check_rotation(self.content, "جدة")
        agents.check_rotation({"title": "تجربة الطقس", "source": "synthetic-fixture"}, "جدة")

    def test_proofreading_and_translation(self):
        agents.proofread(self.content["body"])
        for bad in ("الحرارة  25", "الحرارة ، 25", "الحرارة؟؟", "الحـرارة", " الحرارة"):
            with self.subTest(bad=bad), self.assertRaises(agents.CheckRejected):
                agents.proofread(bad)
        agents.translation_requirement(self.content)
        with self.assertRaises(agents.CheckRejected):
            agents.translation_requirement({**self.content, "body": "Temperature 25"})

    def test_voice_refuses_any_audio_claim(self):
        agents.check_voice({"qa": {"audio": agents.SILENT}})
        for qa in ({}, {"audio": "generated_ambient"}):
            with self.subTest(qa=qa), self.assertRaises(agents.CheckRejected):
                agents.check_voice({"qa": qa})

    def test_media_wants_the_declared_fast_start_mp4(self):
        data = runs.TINY_MP4
        result = {"video_sha256": hashlib.sha256(data).hexdigest(), "qa": {"bytes": len(data)}}
        self.assertEqual(agents.check_media(result, data)["boxes"], ["ftyp", "moov", "mdat"])
        slow = data[:16] + data[24:] + data[16:24]
        cases = (
            (data + b"x", "digest"),
            (slow, "faststart"),
            (b"not an mp4 at all", "container"),
        )
        for bytes_, why in cases:
            declared = {"video_sha256": hashlib.sha256(bytes_).hexdigest(), "qa": {}}
            if why == "digest":
                declared = result
            with self.subTest(why=why), self.assertRaises(agents.CheckRejected):
                agents.check_media(declared, bytes_)
        self.assertEqual(struct.unpack(">I", data[:4])[0], 16)


class ChainTests(unittest.TestCase):
    def setUp(self):
        runs.StatusAgentRuns.setUp(self)

    fixture = runs.StatusAgentRuns.fixture

    def test_twelve_succeed_in_order_and_final_only_asks_the_owner(self):
        prepared, _root, result = self.fixture()
        record_validation(self.cfg, prepared, result)
        with Repository(self.cfg).connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM agent_runs ORDER BY started_at")]
        self.assertEqual([r["role"] for r in rows], list(agents.CHAIN))
        self.assertTrue(all(r["status"] == "SUCCEEDED" for r in rows))
        self.assertTrue(all(r["contract_sha"] == agents.PLAN_SHA for r in rows))
        final = rows[-1]
        decision = {"delivery_decision": "OWNER_REVIEW_REQUIRED", "external_delivery": False}
        self.assertEqual(final["output_sha"], agents.digest(decision), "guard:final-no-delivery")

    def test_a_render_that_claims_audio_stops_before_quality(self):
        prepared, root, result = self.fixture()
        result["qa"]["audio"] = "generated_ambient"
        (root / "output/render-result.json").write_text(json.dumps(result))
        with self.assertRaises(AgentRejected):
            record_validation(self.cfg, prepared, result)
        with Repository(self.cfg).connect() as db:
            roles = [
                r[0] for r in db.execute("SELECT role FROM agent_runs WHERE status='SUCCEEDED'")
            ]
        self.assertNotIn("quality", roles, "guard:voice-before-quality")

    def test_a_non_mp4_stops_at_media(self):
        prepared, root, result = self.fixture()
        data = b"plain bytes, not a container"
        (root / "output/weather.mp4").write_bytes(data)
        result = copy.deepcopy(result)
        result["video_sha256"] = hashlib.sha256(data).hexdigest()
        (root / "output/render-result.json").write_text(json.dumps(result))
        with self.assertRaises(AgentRejected):
            record_validation(self.cfg, prepared, result)


class LocalToolTests(unittest.TestCase):
    def db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE jobs(id TEXT, state TEXT)")
        db.execute("CREATE TABLE agent_runs(app TEXT, role TEXT, status TEXT, error_code TEXT)")
        return db

    def test_manager_holds_while_a_job_is_active(self):
        db = self.db()
        self.assertEqual(oversight.plan(db)["decision"], "READY")
        db.execute("INSERT INTO jobs VALUES('a','RENDERING')")
        self.assertEqual(oversight.plan(db)["decision"], "HOLD")
        db.execute("UPDATE jobs SET state='COMPLETED'")
        self.assertEqual(oversight.plan(db)["decision"], "READY")

    def test_analyst_counts_outcomes_and_causes(self):
        db = self.db()
        db.executemany(
            "INSERT INTO agent_runs VALUES('status-bot-v2',?,?,?)",
            [("memory", "SUCCEEDED", None), ("memory", "FAILED", "AGENT_CALLBACK_FAILED")],
        )
        db.execute("INSERT INTO agent_runs VALUES('snap-bot-v2','memory','FAILED','X')")
        report = oversight.analyse(db)
        self.assertEqual(report["runs"], 2)
        self.assertEqual(report["outcomes"]["memory"], {"SUCCEEDED": 1, "FAILED": 1})
        self.assertEqual(report["causes"], {"AGENT_CALLBACK_FAILED": 1})

    def test_writer_matches_the_source_wording_and_passes_review(self):
        body = oversight.write(25, 10)
        self.assertEqual(body, "الحرارة 25.0 درجة مئوية\nالرياح 10.0 كم في الساعة")
        content = {"title": "طقس الرياض", "body": body, "source": "open-meteo"}
        self.assertEqual(oversight.review(content, "الرياض")["decision"], "PASS")
        bad = {**content, "body": "Temp  25"}
        problems = oversight.review(bad, "جدة")["problems"]
        self.assertEqual(len(problems), 3)

    def test_fixer_repairs_spacing_but_never_words(self):
        fixed = oversight.fix("الحـرارة  25 ، والرياح!!")
        self.assertEqual(fixed["text"], "الحرارة 25، والرياح!")
        self.assertTrue(fixed["changed"])
        self.assertFalse(oversight.fix("طقس الرياض")["changed"])


if __name__ == "__main__":
    unittest.main()
