import array
import copy
import json
import math
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_did_you_know_render as facts
import test_render_ownership as weather

from snap_bot.fact_audio import ambient_source, measure_audio
from snap_bot.render import render, run, validate


class FactAudioTests(unittest.TestCase):
    def encode_audio(self, profile, duration, path):
        run(
            [
                profile["ffmpeg"],
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-f",
                "lavfi",
                "-i",
                ambient_source(duration),
                "-t",
                str(duration),
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-b:a",
                "128k",
                "-threads",
                "1",
                str(path),
            ],
            30,
        )

    def test_source_rejects_invalid_duration(self):
        for value in (True, 0, 7.9, 12.1, float("nan"), "8"):
            with self.assertRaises(ValueError):
                ambient_source(value)

    def test_generated_audio_loudness_true_peak_and_fades(self):
        profile = facts.fact_profile()
        with tempfile.TemporaryDirectory() as directory:
            for duration in (8, 10, 12):
                with self.subTest(duration=duration):
                    path = Path(directory) / "ambient.m4a"
                    self.encode_audio(profile, duration, path)
                    measured = measure_audio(path, profile, run)
                    self.assertTrue(
                        -19 <= measured["integrated_lufs"] <= -17, "guard:ambient-loudness"
                    )
                    self.assertLessEqual(measured["true_peak_dbtp"], -1.5, "guard:ambient-peak")
                    pcm = subprocess.run(
                        [
                            profile["ffmpeg"],
                            "-v",
                            "error",
                            "-i",
                            str(path),
                            "-map",
                            "0:a:0",
                            "-f",
                            "f32le",
                            "-",
                        ],
                        check=True,
                        capture_output=True,
                    ).stdout
                    samples = array.array("f")
                    samples.frombytes(pcm)

                    def rms(start):
                        values = samples[
                            round(start * 48000) * 2 : round((start + 0.08) * 48000) * 2
                        ]
                        return math.sqrt(sum(x * x for x in values) / len(values))

                    middle = rms(duration / 2)
                    self.assertGreater(middle, 0.02, "guard:ambient-not-silent")
                    self.assertLess(rms(0) / middle, 0.25, "guard:ambient-fade-in")
                    self.assertLess(rms(duration - 0.08) / middle, 0.2, "guard:ambient-fade-out")
                    print("AMBIENT_AUDIO_QA=" + json.dumps({"duration": duration, **measured}))

    def test_phone_simulation_loudness_meets_floor(self):
        profile = facts.fact_profile()
        with tempfile.TemporaryDirectory() as directory:
            for duration in (8, 10, 12):
                path = Path(directory) / "phone.m4a"
                self.encode_audio(profile, duration, path)
                phone = measure_audio(path, profile, run, phone=True)
                self.assertGreaterEqual(phone["integrated_lufs"], -24, "guard:ambient-phone")
                print("PHONE_AUDIO_QA=" + json.dumps({"duration": duration, **phone}))

    def test_validate_enforces_phone_loudness_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"probe")
            full = {"integrated_lufs": -18, "true_peak_dbtp": -11}
            phone = {"integrated_lufs": -24.1, "true_peak_dbtp": -17}
            with (
                patch(
                    "snap_bot.render.run",
                    return_value=SimpleNamespace(stdout=json.dumps(self.probe([self.audio()]))),
                ),
                patch("snap_bot.render.measure_audio", side_effect=[full, phone]) as measurement,
            ):
                with self.assertRaises(ValueError):
                    validate(path, facts.fact_profile(), fact=True)
                self.assertEqual(measurement.call_args.kwargs, {"phone": True})

    def probe(self, audio):
        return {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1080,
                    "height": 1920,
                    "pix_fmt": "yuv420p",
                    "duration": "8.0",
                },
                *audio,
            ],
            "format": {"duration": "8.0"},
        }

    def audio(self):
        return {
            "codec_type": "audio",
            "codec_name": "aac",
            "sample_rate": "48000",
            "channels": 2,
            "channel_layout": "stereo",
            "duration": "8.0",
        }

    def test_weather_rejects_any_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"probe")
            with (
                patch(
                    "snap_bot.render.run",
                    return_value=SimpleNamespace(stdout=json.dumps(self.probe([self.audio()]))),
                ),
                self.assertRaises(ValueError, msg="guard:weather-no-audio"),
            ):
                validate(path, facts.fact_profile())

    def test_fact_requires_exact_audio_codec_channels_rate_and_duration(self):
        audio = self.audio()
        invalid = [[], [audio, audio]]
        for key, value in (
            ("codec_name", "mp3"),
            ("sample_rate", "44100"),
            ("channels", 1),
            ("channel_layout", "mono"),
            ("duration", "8.11"),
            ("duration", "nan"),
        ):
            invalid.append([{**audio, key: value}])
        missing = audio.copy()
        missing.pop("duration")
        invalid.append([missing])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"probe")
            for streams in invalid:
                with (
                    self.subTest(audio=streams),
                    patch(
                        "snap_bot.render.run",
                        return_value=SimpleNamespace(stdout=json.dumps(self.probe(streams))),
                    ),
                    self.assertRaises(ValueError),
                ):
                    validate(path, facts.fact_profile(), fact=True)

    def test_validate_enforces_encoded_loudness_and_peak(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"probe")
            for loudness, peak in ((-19.1, -5), (-16.9, -5), (-18, -1.4)):
                with (
                    patch(
                        "snap_bot.render.run",
                        return_value=SimpleNamespace(stdout=json.dumps(self.probe([self.audio()]))),
                    ),
                    patch(
                        "snap_bot.render.measure_audio",
                        return_value={"integrated_lufs": loudness, "true_peak_dbtp": peak},
                    ),
                    self.assertRaises(ValueError),
                ):
                    validate(path, facts.fact_profile(), fact=True)

    def test_measurement_rejects_missing_or_nonfinite_ebur128_summary(self):
        for text in (
            "",
            "Summary:\n I: -inf LUFS\n Peak: -inf dBFS",
            "Summary:\n I: -18 LUFS\n Peak: inf dBFS",
        ):
            with self.assertRaises(ValueError):
                measure_audio(
                    "unused", facts.fact_profile(), lambda *args: SimpleNamespace(stderr=text)
                )

    def test_independent_fact_renders_are_byte_identical_and_have_only_lavfi_inputs(self):
        digests = []
        content = None
        encodes = []

        def observed(args, timeout):
            if "lavfi" in args and str(args[-1]).endswith(".partial.mp4"):
                encodes.append(args)
            return run(args, timeout)

        for _ in range(2):
            fixture = facts.FactRendererTests()
            fixture.setUp()
            f = fixture.fixture
            try:
                if content is None:
                    content = copy.deepcopy(f.content)
                else:
                    fixture.set_content(content)
                f.settings["profile"] = facts.fact_profile()
                with (
                    patch("snap_bot.render.proof", return_value=weather.ISOLATION),
                    patch("snap_bot.render.run", side_effect=observed),
                ):
                    result = render(
                        SimpleNamespace(app="snap-bot-v2"), f.settings, f.job, f.digest, 1
                    )
                digests.append(result["video_sha256"])
                self.assertEqual(result["qa"]["audio"], "generated_ambient")
            finally:
                fixture.doCleanups()
        self.assertEqual(len(encodes), 2)  # Independent encodes, never an immutable-output retry.
        self.assertEqual(digests[0], digests[1], "guard:ambient-video-determinism")
        for args in encodes:
            inputs = [i for i, value in enumerate(args) if value == "-i"]
            self.assertEqual(len(inputs), 2)
            self.assertTrue(all(args[i - 2 : i] == ["-f", "lavfi"] for i in inputs))
            self.assertEqual(args[inputs[1] + 1], ambient_source(8))
            for key, value in (("-c:a", "aac"), ("-ar", "48000"), ("-ac", "2"), ("-b:a", "128k")):
                self.assertEqual(args[args.index(key) + 1], value)
        print("DETERMINISTIC_FACT_VIDEO_SHA256=" + digests[0])

    def test_actual_weather_video_has_no_audio(self):
        fixture = weather.RenderOwnershipTests()
        fixture.setUp()
        try:
            fixture.settings["profile"] = weather.local_profile()
            with patch("snap_bot.render.proof", return_value=weather.ISOLATION):
                render(
                    SimpleNamespace(app="snap-bot-v2"),
                    fixture.settings,
                    fixture.job,
                    fixture.digest,
                    1,
                )
            result = run(
                [
                    fixture.settings["profile"]["ffprobe"],
                    "-v",
                    "error",
                    "-show_streams",
                    "-of",
                    "json",
                    str(fixture.spool() / "output/weather.mp4"),
                ],
                30,
            )
            self.assertFalse(
                any(s["codec_type"] == "audio" for s in json.loads(result.stdout)["streams"])
            )
        finally:
            fixture.doCleanups()
