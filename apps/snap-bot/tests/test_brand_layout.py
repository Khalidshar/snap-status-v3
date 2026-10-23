import itertools
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from snap_bot import fact_layout as layout
from snap_bot.did_you_know import load_facts, make_content
from snap_bot.fact_motion import background_filters
from snap_bot.weather import content_hash


def compact(text):
    """Code shape without formatting: no whitespace, one quote style, no trailing commas."""
    import re

    return re.sub(r",(?=[)\]])", "", re.sub(r"\s+", "", text).replace('"', "'"))


class BrandLayoutTests(unittest.TestCase):
    def content(self):
        return make_content(load_facts()[0], time.time())

    def test_identity_has_no_old_studio_header(self):
        labels = layout.fact_labels(self.content(), {"fact_line_width": 20})
        self.assertEqual(labels[0][0], layout.drawing_text("روّق مع خالد"), "guard:brand-identity")
        self.assertFalse(any("SNAP STUDIO" in label[0] for label in labels), "guard:no-old-brand")

    def test_tag_is_exact_ltr_not_reversed(self):
        labels = layout.fact_labels(self.content(), {"fact_line_width": 20})
        self.assertEqual(labels[1][0], "@rawwag_khalid", "guard:tag-order")
        self.assertEqual(labels[1][4], "latin", "guard:tag-role")
        self.assertEqual(
            layout.display_text(labels[1][0], labels[1][4]),
            "\u202a@rawwag_khalid\u202c",
            "guard:tag-ltr",
        )

    def test_every_body_line_uses_equal_baseline_spacing(self):
        for fact in load_facts():
            labels = layout.fact_labels(make_content(fact, time.time()), {"fact_line_width": 20})
            body = labels[3:-2]
            anchors = []
            for line in body:
                expression = getattr(layout, "label_y", lambda label: str(label[2]))(line)
                self.assertEqual(expression, f"{line[2] + line[1]}-ascent", "guard:baseline-anchor")
                anchors.append(line[2] + line[1])
            self.assertTrue(
                all(b - a == 115 for a, b in itertools.pairwise(anchors)),
                "guard:equal-baseline-spacing",
            )
            self.assertLessEqual(len(body), 5)
            self.assertTrue(all(len(line[0]) <= 20 for line in body))
        self.assertEqual(layout.SAFE, (130, 170, 950, 1580))

    def test_official_palette_and_gradient_are_used(self):
        labels = layout.fact_labels(self.content(), {"fact_line_width": 20})
        self.assertEqual(labels[0][3], "0xD6B26C", "guard:brand-gold")
        self.assertEqual(labels[2][3], "0xD6B26C", "guard:title-gold")
        self.assertTrue(all(label[3] == "0xF6EEDE" for label in labels[3:]), "guard:body-cream")
        filters = background_filters(layout.card_filters())
        self.assertIn(
            "geq=r='257*(11+(6-11)*Y/(H-1))':g='257*(61+(42-61)*Y/(H-1))':b='257*(46+(32-46)*Y/(H-1))'",
            filters,
            "guard:emerald-gradient",
        )
        self.assertFalse(any("0x142D41" in value or "0x76E4C2" in value for value in filters))

    def test_renderer_uses_baseline_and_palette_helpers(self):
        import inspect

        from snap_bot import render

        source = compact(inspect.getsource(render.render))
        self.assertIn(
            compact("filters=card_filters(profile['fps'])"), source, "guard:render-palette"
        )
        self.assertIn(
            compact("filters=background_filters(filters)"), source, "guard:render-fact-gradient"
        )
        self.assertIn(compact("label_y(label) if is_fact"), source, "guard:render-baseline")
        self.assertNotIn("SNAP STUDIO", source, "guard:no-weather-old-brand")

    def test_identity_does_not_mutate_fact_or_hash(self):
        content = self.content()
        original = content.copy()
        layout.fact_labels(content, {"fact_line_width": 20})
        self.assertEqual(content, original)
        self.assertEqual(content["content_hash"], content_hash(content))

    def test_gradient_is_cached_with_explicit_duration(self):
        filters = background_filters(layout.card_filters(30))
        self.assertIn("loop=loop=-1:size=1:start=0", filters, "guard:gradient-cached")
        self.assertIn("setpts=N/(30*TB)", filters, "guard:gradient-clock")
        self.assertLess(
            filters.index("format=yuv420p"), filters.index("loop=loop=-1:size=1:start=0")
        )
        import inspect

        from snap_bot import render

        self.assertIn(
            compact("'-t',str(profile['duration_seconds'])"),
            compact(inspect.getsource(render.render)),
            "guard:bounded-video-duration",
        )
        for invalid in (0, 61, True):
            with self.assertRaises(ValueError):
                layout.card_filters(invalid)

    def test_background_rejects_changed_card_shape(self):
        for fps in range(1, 61):
            original = layout.card_filters(fps)
            background_filters(original)
            self.assertEqual(original, layout.card_filters(fps))
        original = layout.card_filters()
        invalid = [[], original[:-1], [*original, "null"]]
        for i in range(len(original)):
            changed = original.copy()
            changed[i] += "changed"
            invalid.append(changed)
            changed = original.copy()
            changed[i] = None
            invalid.append(changed)
        for i in range(len(original) - 1):
            changed = original.copy()
            changed[i], changed[i + 1] = changed[i + 1], changed[i]
            invalid.append(changed)
        for clock in ("setpts=N/(0*TB)", "setpts=N/(61*TB)", "setpts=N/(01*TB)"):
            changed = original.copy()
            changed[4] = clock
            invalid.append(changed)
        for changed in invalid:
            with self.subTest(filters=changed):
                with self.assertRaises(ValueError, msg="guard:background-shape"):
                    background_filters(changed)


@unittest.skipUnless(
    os.name == "posix" and os.environ.get("MADAR_BRAND_MUTATION") != "1",
    "Linux FFmpeg brand pixel acceptance",
)
class BrandPixelTests(unittest.TestCase):
    def test_tag_pixels_match_unshaped_ltr_reference(self):
        from test_did_you_know_render import fact_profile

        from snap_bot.font_admission import font_paths

        p = fact_profile()
        self.assertIsNotNone(p)
        font = font_paths(p)[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tag.txt"

            def pixels(text, shaping):
                path.write_text(text, encoding="utf-8")
                value = f"drawtext=fontfile={font}:textfile={path}:expansion=none:text_shaping={shaping}:fontsize=38:fontcolor=white:x=(w-text_w)/2:y=290"
                return subprocess.run(
                    [
                        p["ffmpeg"],
                        "-v",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        "color=c=black:s=1080x1920:r=1:d=1",
                        "-vf",
                        "format=rgb24," + value + ",format=gray",
                        "-frames:v",
                        "1",
                        "-threads",
                        "1",
                        "-f",
                        "rawvideo",
                        "-",
                    ],
                    check=True,
                    capture_output=True,
                ).stdout

            actual = pixels(layout.display_text("@rawwag_khalid", "latin"), 1)
            self.assertEqual(actual, pixels("@rawwag_khalid", 0), "guard:real-tag-ltr")
            self.assertNotEqual(actual, pixels("rawwag_khalid@", 0))

    def test_gradient_endpoints_are_actual_emerald_pixels(self):
        from test_did_you_know_render import fact_profile

        p = fact_profile()
        self.assertIsNotNone(p)
        value = ",".join(background_filters(layout.card_filters()))
        frame = subprocess.run(
            [
                p["ffmpeg"],
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=1080x1920:r=1:d=1",
                "-vf",
                value,
                "-frames:v",
                "1",
                "-threads",
                "1",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "-",
            ],
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(len(frame), 1080 * 1920 * 3)
        # Average fixed grain over a text-free patch; keep the same RGB tolerance.
        for rows, expected in ((range(8), (11, 61, 46)), (range(1912, 1920), (6, 42, 32))):
            actual = [
                sum(frame[(y * 1080 + x) * 3 + c] for y in rows for x in range(64)) / (8 * 64)
                for c in range(3)
            ]
            self.assertTrue(
                all(abs(a - b) <= 2 for a, b in zip(actual, expected, strict=False)),
                "guard:gradient-pixels",
            )


if __name__ == "__main__":
    unittest.main()
