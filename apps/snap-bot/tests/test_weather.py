import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from snap_bot.render import ensure_glyphs, proof, validate
from snap_bot.weather import SourceInvalid, content_hash, fetch_weather, parse
from studio_core.network import NetworkError, NetworkResponse


class WeatherTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 1, 1, tzinfo=UTC).timestamp()
        self.sources = [
            {
                "id": "open-meteo",
                "url": "https://api.open-meteo.com/test",
                "attribution": "Open-Meteo / CC BY 4.0",
                "license": "https://creativecommons.org/licenses/by/4.0/",
            },
            {
                "id": "met-no",
                "url": "https://api.met.no/test",
                "attribution": "MET Norway / CC BY 4.0",
                "license": "https://creativecommons.org/licenses/by/4.0/",
            },
        ]
        self.primary = {
            "current": {"temperature_2m": 28.6, "wind_speed_10m": 8.4, "time": "2026-10-01T04:00"},
            "current_units": {"temperature_2m": "°C", "wind_speed_10m": "km/h"},
        }
        self.secondary = {
            "properties": {
                "meta": {"units": {"air_temperature": "celsius", "wind_speed": "m/s"}},
                "timeseries": [
                    {
                        "time": "2026-10-01T01:00:00Z",
                        "data": {"instant": {"details": {"air_temperature": 29, "wind_speed": 2}}},
                    }
                ],
            }
        }
        self.settings = {"city": "الرياض", "ttl_seconds": 3600, "sources": self.sources}

    def response(self, value):
        return NetworkResponse(200, "application/json", json.dumps(value).encode(), "fixture")

    def test_primary_values_provenance_and_expiry(self):
        c = parse(self.sources[0], json.dumps(self.primary), "الرياض", self.now, 3600)
        self.assertEqual(c["temperature_c"], 28.6)
        self.assertFalse(c["sacred"])
        self.assertEqual(c["expires_at"], self.now + 3600)
        self.assertEqual(c["content_hash"], content_hash(c))

    def test_same_model_content_hash_ignores_fetch_time(self):
        a = parse(self.sources[0], json.dumps(self.primary), "الرياض", self.now, 3600)
        b = parse(self.sources[0], json.dumps(self.primary), "الرياض", self.now + 60, 3600)
        self.assertEqual(a["content_hash"], b["content_hash"])
        self.assertNotEqual(a["fetched_at"], b["fetched_at"])

    def test_fallback_converts_mps_to_kmh(self):
        c = parse(self.sources[1], json.dumps(self.secondary), "الرياض", self.now, 3600)
        self.assertEqual(c["wind_kmh"], 7.2)

    def test_source_timeout_uses_independent_fallback(self):
        client = Mock()
        client.get.side_effect = [NetworkError("TIMEOUT"), self.response(self.secondary)]
        content, failures = fetch_weather(client, self.settings, self.now)
        self.assertEqual(content["source"], "met-no")
        self.assertEqual(failures[0]["source"], "open-meteo")

    def test_corrupt_primary_uses_fallback(self):
        client = Mock()
        client.get.side_effect = [self.response({"bad": "data"}), self.response(self.secondary)]
        self.assertEqual(fetch_weather(client, self.settings, self.now)[0]["source"], "met-no")

    def test_html_response_rejected(self):
        client = Mock()
        client.get.return_value = NetworkResponse(200, "text/html", b"{}", "fixture")
        with self.assertRaises(SourceInvalid):
            fetch_weather(client, self.settings, self.now)

    def test_all_sources_fail_closed(self):
        client = Mock()
        client.get.side_effect = NetworkError("503")
        with self.assertRaises(SourceInvalid):
            fetch_weather(client, self.settings, self.now)

    def test_stale_model_rejected(self):
        with self.assertRaises(SourceInvalid):
            parse(self.sources[0], json.dumps(self.primary), "الرياض", self.now + 7200, 3600)

    def test_invalid_units_nan_and_range_rejected(self):
        for value in (float("nan"), None, True, 999):
            with self.subTest(value=value), self.assertRaises(SourceInvalid):
                changed = json.loads(json.dumps(self.primary))
                changed["current"]["temperature_2m"] = value
                parse(self.sources[0], json.dumps(changed), "الرياض", self.now, 3600)
        self.primary["current_units"]["temperature_2m"] = "F"
        with self.assertRaises(SourceInvalid):
            parse(self.sources[0], json.dumps(self.primary), "الرياض", self.now, 3600)

    def test_qa_rejects_wrong_resolution(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "file.mp4"
            path.write_bytes(b"not-valid")
            profile = {
                "ffprobe": "ffprobe",
                "ffmpeg": "ffmpeg",
                "width": 1080,
                "height": 1920,
                "duration_seconds": 8,
                "max_bytes": 50000000,
                "timeout_seconds": 240,
            }
            fake = Mock(
                stdout=json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "video",
                                "width": 720,
                                "height": 1280,
                                "codec_name": "h264",
                                "pix_fmt": "yuv420p",
                            }
                        ],
                        "format": {"duration": "8"},
                    }
                )
            )
            with (
                patch("snap_bot.render.run", return_value=fake),
                self.assertRaisesRegex(ValueError, "profile"),
            ):
                validate(path, profile)

    def test_renderer_refuses_unisolated_process(self):
        with self.assertRaisesRegex(RuntimeError, "isolation"):
            proof("/nonexistent.sock")

    def test_missing_degree_symbol_blocks_render(self):
        with self.assertRaisesRegex(ValueError, "glyph"):
            ensure_glyphs("29.5°", "20-7e")

    def test_ascii_and_degree_covered_by_separate_font(self):
        ensure_glyphs("SNAP STUDIO / V2 29.5°", "20-7e a0-ff")


if __name__ == "__main__":
    unittest.main()
