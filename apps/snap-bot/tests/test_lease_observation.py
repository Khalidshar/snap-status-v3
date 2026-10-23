import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from snap_bot.agent_telemetry import TelemetryReconciliationRequired
from snap_bot.service import execute
from studio_core.config import Config
from studio_core.jobs import Repository


class LeaseRecoveryObservationTests(unittest.TestCase):
    def test_crash_then_reclaimed_lease_has_explicit_reconciliation_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            config = replace(
                Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"),
                data_root=Path(temp),
            )
            repo = Repository(config)
            job, _ = repo.enqueue(
                "synthetic-recovery",
                "synthetic-owner",
                "weather",
                "NORMAL",
                "synthetic-input",
                time.time() + 600,
            )
            first = repo.claim()
            script = """import os,sys
from pathlib import Path
from types import SimpleNamespace
import snap_bot.agent_telemetry as telemetry
telemetry.NODE_EXECUTABLE=sys.argv[3]
observe=telemetry.observe
config=SimpleNamespace(data_root=Path(sys.argv[1]),app='snap-bot-v2')
observe(config,'fact_source',sys.argv[2],lambda:os._exit(70))
"""
            crashed = subprocess.run(
                [sys.executable, "-c", script, temp, job["id"], shutil.which("node")],
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(crashed.returncode, 70, crashed.stderr)
            with repo.connect() as db:
                db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job["id"]))
            recovered = repo.claim()
            self.assertNotEqual(first["lease_token"], recovered["lease_token"])
            self.assertTrue(
                any(json.loads(row["details"]).get("recovered") for row in repo.timeline(job["id"]))
            )
            with (
                patch("snap_bot.service.fetch_weather") as fetch,
                patch("snap_bot.agent_telemetry.NODE_EXECUTABLE", shutil.which("node")),
            ):
                with self.assertRaises(TelemetryReconciliationRequired):
                    execute(config, {}, "synthetic-gateway-value" * 2, claim=recovered)
            fetch.assert_not_called()
            self.assertEqual(repo.get(job["id"])["state"], "FAILED")
            states = [
                json.loads(row["details"])
                for row in repo.timeline(job["id"])
                if row["event"] == "job.state"
            ]
            self.assertEqual(states[-1]["code"], "TELEMETRY_RECONCILIATION_REQUIRED")
            journal = config.data_root / "madar/snap-bot-v2/events.jsonl"
            self.assertEqual(
                [json.loads(line)["status"] for line in journal.read_text().splitlines()],
                ["RUNNING"],
            )
