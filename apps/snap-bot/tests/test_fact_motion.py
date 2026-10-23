import itertools
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_did_you_know_render as fixtures
from test_render_ownership import ISOLATION

from snap_bot.did_you_know import load_facts, make_content
from snap_bot.fact_layout import card_filters, display_text, fact_labels, label_y, measured_box
from snap_bot.fact_motion import (
    background_filters,
    glow_filter,
    glow_position,
    label_filter,
    reveal_windows,
)
from snap_bot.font_admission import font_paths
from snap_bot.render import render, validate


class FactMotionTests(unittest.TestCase):
    def test_encoded_fact_cannot_exceed_twelve_seconds(self):
        profile = fixtures.fact_profile()
        profile["duration_seconds"] = 12
        probe = {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1080,
                    "height": 1920,
                    "codec_name": "h264",
                    "pix_fmt": "yuv420p",
                }
            ],
            "format": {"duration": "12.01"},
        }
        with patch("snap_bot.render.run", return_value=SimpleNamespace(stdout=json.dumps(probe))):
            with self.assertRaisesRegex(ValueError, "duration limit"):
                validate(Path("unused.mp4"), profile, fact=True)

    def test_reveal_order_and_complete_reading_hold_for_every_fact(self):
        for fact in load_facts():
            labels = fact_labels(make_content(fact, time.time()), fixtures.fact_profile())
            for duration in (8, 10, 12):
                windows = reveal_windows(len(labels), duration)
                self.assertEqual(windows[:3], [(0, 0.6), (0, 0.6), (0.6, 1.2)])
                for i, (start, end) in enumerate(windows[3:-2]):
                    self.assertAlmostEqual(start, 1.2 + 0.5 * i)
                    self.assertAlmostEqual(end - start, 0.5)
                self.assertEqual(windows[-2][0], windows[-3][1])
                self.assertEqual(windows[-1][0], windows[-2][1])
                self.assertLessEqual(max(end for _, end in windows), duration - 1.5)
        for count, duration in ((5, 8), (11, 8), (9, 7.9), (9, 12.1)):
            with self.assertRaises(ValueError):
                reveal_windows(count, duration)

    def test_glow_is_cached_subtle_and_frozen_during_reading_hold(self):
        background = background_filters(card_filters())
        self.assertLess(
            background.index("noise=alls=3:allf=u:all_seed=20261004"),
            background.index("loop=loop=-1:size=1:start=0"),
        )
        self.assertEqual(background, background_filters(card_filters()))
        filters = glow_filter(24, 8)
        self.assertIn("trim=end_frame=1", filters)
        self.assertIn("loop=loop=-1:size=1:start=0", filters)
        self.assertIn("a='floor(6*max", filters)
        self.assertIn("mod(abs(sin(X*12.9898+Y*78.233)*43758.5453),1)", filters)
        self.assertIn("min(t,6.5)", filters)
        self.assertEqual(glow_position(6.5, 8), glow_position(7.5, 8))
        self.assertEqual(glow_position(6.5, 8), glow_position(8, 8))
        self.assertNotEqual(glow_position(0, 8), glow_position(4, 8))

    def test_each_element_has_full_reference_pixels_inside_safe_at_hold_endpoints(self):
        profile = fixtures.fact_profile()
        self.assertIsNotNone(profile)
        fonts = dict(zip(("arabic", "latin"), font_paths(profile), strict=False))
        fact = max(load_facts(), key=lambda item: len(item["text"]))
        labels = fact_labels(make_content(fact, time.time()), profile)
        windows = reveal_windows(len(labels), 8)
        with tempfile.TemporaryDirectory() as directory:
            for i, label in enumerate(labels):
                path = Path(directory) / "label.txt"
                path.write_text(display_text(label[0], label[4]), encoding="utf-8")
                static = (
                    f"drawtext=fontfile={fonts[label[4]]}:textfile={path}:"
                    f"expansion=none:text_shaping=1:fontsize={label[1]}:"
                    f"fontcolor={label[3]}:x=(w-text_w)/2:y={label_y(label)}"
                )

                def pixels(value, t):
                    return subprocess.run(
                        [
                            profile["ffmpeg"],
                            "-v",
                            "error",
                            "-nostdin",
                            "-f",
                            "lavfi",
                            "-i",
                            "color=c=black:s=1080x1920:r=24:d=1",
                            "-vf",
                            f"setpts=PTS+{t}/TB,format=rgb24," + value + ",format=gray",
                            "-frames:v",
                            "1",
                            "-threads",
                            "1",
                            "-filter_threads",
                            "1",
                            "-pix_fmt",
                            "gray",
                            "-f",
                            "rawvideo",
                            "-",
                        ],
                        check=True,
                        capture_output=True,
                        timeout=30,
                    ).stdout

                reference = pixels(static, 0)
                measured_box(reference)  # Actual shaped pixels must remain inside unchanged SAFE.
                for t in (6.5, 8 - 1 / 24):
                    with self.subTest(element=i, t=t):
                        self.assertEqual(pixels(label_filter(static, windows[i]), t), reference)
                if i == 0:
                    self.assertNotEqual(pixels(label_filter(static, windows[i]), 0.3), reference)

    def test_actual_video_duration_ambient_and_all_final_frames_static(self):
        fixture = fixtures.FactRendererTests()
        fixture.setUp()
        f = fixture.fixture
        try:
            f.settings["profile"] = fixtures.fact_profile()
            with patch("snap_bot.render.proof", return_value=ISOLATION):
                result = render(SimpleNamespace(app="snap-bot-v2"), f.settings, f.job, f.digest, 1)
            self.assertEqual(result["qa"]["duration"], 8)
            self.assertLessEqual(result["qa"]["duration"], 12)
            self.assertEqual(result["qa"]["audio"], "generated_ambient")
            video = f.spool() / "output/did-you-know.mp4"
            frames = subprocess.run(
                [
                    f.settings["profile"]["ffmpeg"],
                    "-v",
                    "error",
                    "-ss",
                    "6.5",
                    "-i",
                    str(video),
                    "-vf",
                    "scale=270:480",
                    "-threads",
                    "1",
                    "-pix_fmt",
                    "gray",
                    "-f",
                    "rawvideo",
                    "-",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            ).stdout
            size = 270 * 480
            self.assertEqual(len(frames), size * 36)
            reference = frames[:size]
            for offset in range(size, len(frames), size):
                difference = [
                    abs(a - b)
                    for a, b in zip(reference, frames[offset : offset + size], strict=False)
                ]
                self.assertLessEqual(max(difference), 4)
                # Fixed source grain can have tiny inter-frame H.264 residuals.
                self.assertLess(sum(difference) / size, 0.2)
        finally:
            fixture.doCleanups()

    def test_background_row_steps_and_text_contrast_at_seven_point_five_seconds(self):
        fixture = fixtures.FactRendererTests()
        fixture.setUp()
        f = fixture.fixture
        try:
            profile = f.settings["profile"] = fixtures.fact_profile()
            with patch("snap_bot.render.proof", return_value=ISOLATION):
                result = render(SimpleNamespace(app="snap-bot-v2"), f.settings, f.job, f.digest, 1)
            video = f.spool() / "output/did-you-know.mp4"
            self.assertLessEqual(video.stat().st_size, profile["max_bytes"])

            def gray(arguments):
                return subprocess.run(
                    [
                        profile["ffmpeg"],
                        "-v",
                        "error",
                        "-nostdin",
                        *arguments,
                        "-frames:v",
                        "1",
                        "-threads",
                        "1",
                        "-filter_threads",
                        "1",
                        "-pix_fmt",
                        "gray",
                        "-f",
                        "rawvideo",
                        "-",
                    ],
                    check=True,
                    capture_output=True,
                    timeout=30,
                ).stdout

            actual = gray(["-ss", "7.5", "-i", str(video)])
            self.assertEqual(len(actual), 1080 * 1920)
            # Mean rows average out intentional grain. Both strips contain no text.
            for x0, x1, y0, y1 in ((10, 60, 40, 1880), (300, 780, 565, 650)):
                rows = [
                    sum(actual[y * 1080 + x0 : y * 1080 + x1]) / (x1 - x0) for y in range(y0, y1)
                ]
                jump = max(abs(a - b) for a, b in itertools.pairwise(rows))
                self.assertLessEqual(jump, 1.5, "Background row discontinuity on 0..255 luma")
            labels = fact_labels(f.content, profile)
            fonts = dict(zip(("arabic", "latin"), font_paths(profile), strict=False))
            filters = card_filters(profile["fps"])
            for i, label in enumerate(labels):
                path = f.spool() / f"contrast-{i}.txt"
                path.write_text(display_text(label[0], label[4]), encoding="utf-8")
                filters.append(
                    f"drawtext=fontfile={fonts[label[4]]}:textfile={path}:"
                    f"expansion=none:text_shaping=1:fontsize={label[1]}:"
                    f"fontcolor={label[3]}:x=(w-text_w)/2:y={label_y(label)}"
                )
            reference = gray(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=1080x1920:r=24:d=1",
                    "-vf",
                    ",".join(filters),
                ]
            )
            # Solid glyph cores retain foreground contrast against the unchanged palette.
            for box in result["safe_area"]["boxes"]:
                x0, y0 = box["x"], box["y"]
                x1, y1 = x0 + box["width"], y0 + box["height"]
                positions = [y * 1080 + x for y in range(y0, y1) for x in range(x0, x1)]
                peak = max(reference[p] for p in positions)
                core = [p for p in positions if reference[p] >= peak - 2]
                y = (y0 + y1) // 2

                def contrast(frame):
                    return (
                        sum(frame[p] for p in core) / len(core)
                        - sum(frame[y * 1080 + 10 : y * 1080 + 60]) / 50
                    )

                self.assertLessEqual(abs(contrast(actual) - contrast(reference)), 3)
        finally:
            fixture.doCleanups()
