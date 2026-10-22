import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from snap_bot.schedule_once import enqueue
from studio_core.config import Config


class ScheduleTests(unittest.TestCase):
    def test_due_request_is_durable_and_replay_reuses_job(self):
        with tempfile.TemporaryDirectory() as temp:
            config = replace(
                Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"),
                data_root=Path(temp),
                owners={"telegram": ("42",)},
            )
            due = int(time.time()) - 1
            first = enqueue(config, "s" * 32, "42", "scheduled-test", due)
            second = enqueue(config, "s" * 32, "42", "scheduled-test", due)
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(first["job_id"], second["job_id"])

    def test_early_and_stale_schedules_do_not_enqueue(self):
        with tempfile.TemporaryDirectory() as temp:
            config = replace(
                Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"),
                data_root=Path(temp),
                owners={"telegram": ("42",)},
            )
            with self.assertRaises(ValueError):
                enqueue(config, "s" * 32, "42", "early", 1000, 999)
            self.assertEqual(
                enqueue(config, "s" * 32, "42", "stale", 1000, 1600)["state"], "MISSED"
            )
            self.assertFalse((Path(temp) / "jobs.sqlite").exists())

    def test_destination_cannot_be_changed(self):
        with tempfile.TemporaryDirectory() as temp:
            config = replace(
                Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"),
                data_root=Path(temp),
                owners={"telegram": ("42",)},
            )
            with self.assertRaises(ValueError):
                enqueue(config, "s" * 32, "99", "wrong-owner", 1000, 1001)
