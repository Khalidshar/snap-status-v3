import hashlib
import json
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from status_bot.pipeline import prepare, record_validation
from status_bot.render import render
from status_bot.weather import SourceInvalid, content_hash, parse
from studio_core.config import Config
from studio_core.jobs import Repository
from studio_core.storage import Storage


class FixtureClient:
    def get(self, url):
        payload = {
            "current_units": {"temperature_2m": "°C", "wind_speed_10m": "km/h"},
            "current": {
                "time": datetime.now(ZoneInfo("Asia/Riyadh")).replace(tzinfo=None).isoformat(),
                "temperature_2m": 30,
                "wind_speed_10m": 12,
            },
        }
        return SimpleNamespace(content_type="application/json", body=json.dumps(payload).encode())


class StatusWeather(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = replace(
            Config.load(Path(__file__).parents[3] / "config/status/weather.toml"),
            data_root=self.root / "data",
        )
        self.settings = {
            "city": "الرياض",
            "ttl_seconds": 3600,
            "sources": [
                {
                    "id": "open-meteo",
                    "url": "https://api.open-meteo.com/fixture",
                    "license": "fixture-license",
                    "attribution": "Fixture only",
                }
            ],
        }
        self.secret = "status-weather-fixture-gateway-secret-only"

    def test_prepare_has_independent_app_and_no_external_delivery(self):
        value = prepare(
            self.cfg, self.settings, FixtureClient(), self.secret, "weather_1", synthetic=True
        )
        self.assertEqual(value["claim"]["app"], "status-bot-v2")
        self.assertFalse(value["external_delivery"])
        self.assertFalse(value["content"]["sacred"])
        self.assertEqual(value["content"]["source"], "synthetic-fixture")
        self.assertEqual(Repository(self.cfg).get(value["claim"]["id"])["state"], "RENDERING")

    def test_duplicate_event_does_not_render_again(self):
        prepare(self.cfg, self.settings, FixtureClient(), self.secret, "weather_1", synthetic=True)
        with self.assertRaises(ValueError):
            prepare(
                self.cfg, self.settings, FixtureClient(), self.secret, "weather_1", synthetic=True
            )

    def test_qa_record_keeps_manual_review_pending(self):
        value = prepare(
            self.cfg, self.settings, FixtureClient(), self.secret, "weather_1", synthetic=True
        )
        video = b"unit-fixture-bytes-not-a-real-render"
        (Storage(self.cfg).workspace(value["claim"]["id"]) / "output/weather.mp4").write_bytes(
            video
        )
        result = {
            "job_id": value["claim"]["id"],
            "content_hash": value["content"]["content_hash"],
            "video_sha256": hashlib.sha256(video).hexdigest(),
            "qa": {"result": "PASS", "visual_review": "PENDING"},
        }
        (
            Storage(self.cfg).workspace(value["claim"]["id"]) / "output/render-result.json"
        ).write_text(json.dumps(result))
        manifest = record_validation(self.cfg, value, result)
        self.assertEqual(manifest["state"], "VALIDATING")
        self.assertFalse(manifest["external_delivery"])
        self.assertEqual(manifest["delivery"], "NOT_ATTEMPTED")
        self.assertEqual(manifest["publication"], "NOT_IMPLEMENTED")
        self.assertEqual(manifest["destination"], "whatsapp")
        self.assertEqual(manifest["visual_review"], "PENDING")
        self.assertNotIn("TELEGRAM", json.dumps(manifest))
        self.assertEqual(Repository(self.cfg).get(value["claim"]["id"])["state"], "VALIDATING")

    def test_wrong_qa_job_or_content_rejected(self):
        value = prepare(
            self.cfg, self.settings, FixtureClient(), self.secret, "weather_1", synthetic=True
        )
        for job, digest in (
            ("wrong", value["content"]["content_hash"]),
            (value["claim"]["id"], "0" * 64),
        ):
            with self.assertRaises(ValueError):
                record_validation(
                    self.cfg,
                    value,
                    {"job_id": job, "content_hash": digest, "qa": {"result": "PASS"}},
                )

    def test_invalid_weather_never_produces_content(self):
        source = self.settings["sources"][0]
        with self.assertRaises(SourceInvalid):
            parse(source, b"{}", "الرياض", time.time(), 3600)

    def test_renderer_refuses_sacred_input_even_with_matching_hash(self):
        value = prepare(
            self.cfg, self.settings, FixtureClient(), self.secret, "weather_1", synthetic=True
        )
        content = value["content"]
        content["sacred"] = True
        content["content_hash"] = content_hash(content)
        path = Storage(self.cfg).workspace(value["claim"]["id"]) / "input/content.json"
        path.write_text(json.dumps(content))
        with patch("status_bot.render.proof", return_value={}):
            with self.assertRaises(ValueError):
                render(
                    self.cfg,
                    {"egress_socket": self.root / "absent.sock"},
                    value["claim"]["id"],
                    content["content_hash"],
                )

    def test_renderer_refuses_tampered_input_before_tools(self):
        value = prepare(
            self.cfg, self.settings, FixtureClient(), self.secret, "weather_1", synthetic=True
        )
        content = value["content"]
        content["temperature_c"] += 1
        path = Storage(self.cfg).workspace(value["claim"]["id"]) / "input/content.json"
        path.write_text(json.dumps(content))
        with patch("status_bot.render.proof", return_value={}):
            with self.assertRaises(ValueError):
                render(
                    self.cfg,
                    {"egress_socket": self.root / "absent.sock"},
                    value["claim"]["id"],
                    content["content_hash"],
                )

    def test_content_hash_detects_changed_weather(self):
        value = prepare(
            self.cfg, self.settings, FixtureClient(), self.secret, "weather_1", synthetic=True
        )
        content = value["content"]
        content["temperature_c"] += 1
        self.assertNotEqual(content_hash(content), content["content_hash"])


if __name__ == "__main__":
    unittest.main()
