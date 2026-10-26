import hashlib
import json
import os
import struct
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_render_ownership as ownership

from snap_bot.did_you_know import load_facts, make_content
from snap_bot.fact_layout import display_text, drawing_text, fact_labels, measured_box
from snap_bot.render import ensure_glyphs, render, run, video_filename
from snap_bot.render_commit import stage_input
from snap_bot.weather import content_hash


def fact_profile():
    profile = ownership.local_profile()
    if profile is None:
        return None
    pinned = tomllib.loads(
        (Path(__file__).parents[3] / "config/snap/did_you_know.toml").read_text()
    )["slice"]["profile"]
    for key in (
        "font",
        "font_sha256",
        "latin_font",
        "latin_font_sha256",
        "fact_body_font_size",
        "fact_line_width",
    ):
        profile[key] = (
            str(Path(__file__).parents[3] / pinned[key])
            if key in ("font", "latin_font")
            else pinned[key]
        )
    profile["duration_seconds"] = 8
    return profile


class FactLayoutTests(unittest.TestCase):
    def test_drawn_text_omits_marks_without_changing_catalog_or_hash(self):
        self.assertEqual(drawing_text("يومًا أرضيًا تتكوّن"), "يوما أرضيا تتكون")
        self.assertEqual(drawing_text("ا\u0653 ا\u0654 ا\u0655 ه\u0670"), "ا ا ا ه")
        for fact in load_facts():
            content = make_content(fact, time.time())
            before = json.dumps(content, ensure_ascii=False, sort_keys=True)
            for label in fact_labels(content):
                self.assertFalse(
                    any(0x064B <= ord(char) <= 0x0655 or ord(char) == 0x0670 for char in label[0])
                )
            self.assertEqual(json.dumps(content, ensure_ascii=False, sort_keys=True), before)
            self.assertEqual(content["body"], fact["text"])
            self.assertEqual(content["content_hash"], content_hash(content))

    def test_renderer_sets_rtl_embedding_without_reversing_latin_or_numbers(self):
        text = "تتكوّن عناوين IPv6 من 128"
        self.assertEqual(display_text(text, "arabic"), "\u202b" + text + "\u202c")
        self.assertEqual(display_text("@rawwag_khalid", "latin"), "\u202a@rawwag_khalid\u202c")
        content = make_content(next(f for f in load_facts() if f["id"] == "dyk-09"), time.time())
        digest = content["content_hash"]
        for label in fact_labels(content):
            display_text(label[0], label[4])
        self.assertEqual(content["content_hash"], digest)

    def test_body_centered_by_line_count_with_fixed_source_and_footer(self):
        counts = set()
        for fact in load_facts():
            labels = fact_labels(make_content(fact, time.time()))
            body = labels[3:-2]
            counts.add(len(body))
            self.assertAlmostEqual((body[0][2] + body[-1][2] + body[-1][1]) / 2, 930, delta=0.5)
            self.assertEqual(labels[-2][2], 1320)
            self.assertEqual(labels[-1][2], 1480)
            self.assertLessEqual(len(body), 5)
        self.assertGreater(len(counts), 1)

    def test_amiri_comparison_sizes_and_centering(self):
        profile = {"fact_body_font_size": 80, "fact_title_font_size": 104}
        for fact in load_facts():
            labels = fact_labels(make_content(fact, time.time()), profile)
            body = labels[3:-2]
            self.assertEqual(labels[2][1], 104)
            self.assertTrue(all(line[1] == 80 for line in body))
            self.assertAlmostEqual((body[0][2] + body[-1][2] + 80) / 2, 930, delta=0.5)
            self.assertEqual(labels[-2][2], 1320)
            self.assertEqual(labels[-1][2], 1480)

    def pixels(self, x, y, width, height):
        frame = bytearray(1080 * 1920)
        for py in range(y, min(y + height, 1920)):
            frame[py * 1080 + x : py * 1080 + min(x + width, 1080)] = bytes([255]) * min(
                width, 1080 - x
            )
        return frame

    def test_all_fact_lines_bounded_and_no_emoji_drawtext(self):
        for fact in load_facts():
            labels = fact_labels(make_content(fact, time.time()))
            self.assertTrue(all("👻" not in label[0] for label in labels))
            self.assertEqual(labels[-1][0], "تابعني للمزيد")
            self.assertTrue(all(len(label[0]) <= 26 for label in labels[3:-2]))

    def test_missing_glyph_rejected(self):
        with self.assertRaisesRegex(ValueError, "glyph"):
            ensure_glyphs("هل تعلم؟", "20-7e")

    def test_bbox_requires_measurement_and_rejects_overflow(self):
        for pixels in (
            b"",
            bytes(1080 * 1920),
            self.pixels(100, 650, 900, 90),
            self.pixels(490, 1540, 100, 90),
        ):
            with self.assertRaises(ValueError):
                measured_box(pixels)
        self.assertEqual(measured_box(self.pixels(290, 650, 500, 80))["width"], 500)

    def test_type_allowlist_rejects_unknown(self):
        with self.assertRaises(ValueError):
            video_filename("religious")


@unittest.skipUnless(os.name == "posix", "Linux renderer ownership contracts")
class FactRendererTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ownership.RenderOwnershipTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fact = max(load_facts(), key=lambda fact: len(fact["text"]))
        self.set_content(make_content(self.fact, time.time()))
        self.fixture.settings["profile"]["duration_seconds"] = 8
        # Bytes and QA shaped like a real fact render, so the post-render agents can pass.
        import test_owned_contract_adapter as adapter

        self.fixture.video = adapter.faststart_mp4(b"synthetic owned fact render")
        self.qa = {
            "result": "PASS",
            "width": 1080,
            "height": 1920,
            "duration": 8.0,
            "bytes": len(self.fixture.video),
            "codec": "h264",
            "audio": "generated_ambient",
            "audio_qa": {"integrated_lufs": -18.0, "true_peak_dbtp": -3.0, "phone_lufs": -21.0},
            "visual_review": "PENDING",
        }

    def fake_run(self, args, timeout):
        if str(args[-1]).endswith(".preview.png"):
            Path(args[-1]).write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
            return SimpleNamespace(stdout="")
        return self.fixture.fake_run(args, timeout)

    def set_content(self, content):
        f = self.fixture
        path = (
            __import__("pathlib").Path(f.settings["input_root"])
            / f.job
            / "attempts/1/input/content.json"
        )
        path.unlink()
        f.content = content
        f.digest = content["content_hash"]
        stage_input(f.settings, f.repo, *f.args(), content)

    def fake_render(self):
        f = self.fixture
        with (
            patch("snap_bot.render.run", side_effect=self.fake_run),
            patch("snap_bot.render.proof", return_value=ownership.ISOLATION),
            patch("snap_bot.render.validate", return_value=self.qa),
            patch(
                "snap_bot.render.measure_label",
                return_value={"x": 200, "y": 650, "width": 600, "height": 80},
            ),
        ):
            return render(f.config, f.settings, f.job, f.digest, 1)

    def test_db_free_fact_render_and_fenced_adoption(self):
        f = self.fixture
        with patch("studio_core.jobs.Repository", side_effect=AssertionError("Renderer opened DB")):
            result = self.fake_render()
        self.assertEqual(result["artifact"], f"renders/{f.job}/attempts/1/output/did-you-know.mp4")
        f.rendering()
        f.adopt(result)
        self.assertEqual((f.legacy() / "did-you-know.mp4").read_bytes(), f.video)
        self.assertEqual(f.repo.stage_result(f.job, "design")["attempt"], 1)

    def test_wrong_request_hash_rejected(self):
        self.fixture.digest = "0" * 64
        with self.assertRaises(ValueError):
            self.fake_render()

    def test_sacred_unknown_type_and_unreviewed_text_rejected_before_render(self):
        for changed in ({"sacred": True}, {"type": "unknown"}, {"body": "نص لم يراجعه أحد"}):
            content = {**make_content(self.fact, time.time()), **changed}
            content["content_hash"] = content_hash(content)
            self.set_content(content)
            with (
                patch("snap_bot.render.proof", side_effect=AssertionError("Rejected too late")),
                self.assertRaises(ValueError),
            ):
                render(
                    self.fixture.config,
                    self.fixture.settings,
                    self.fixture.job,
                    content["content_hash"],
                    1,
                )

    def test_adoption_rejects_missing_fact_safe_area(self):
        f = self.fixture
        result = self.fake_render()
        f.rendering()
        result.pop("safe_area")
        with self.assertRaises(ValueError):
            f.adopt(result)

    def test_execute_uses_local_source_and_owned_adoption_without_network_or_send(self):
        from pathlib import Path

        from snap_bot.service import execute

        f = self.fixture
        with f.repo.transaction() as db:
            db.execute("UPDATE jobs SET command='did_you_know' WHERE id=?", (f.job,))
        claim = {**f.owner, "command": "did_you_know"}
        input_path = Path(f.settings["input_root"]) / f.job / "attempts/1/input/content.json"
        input_path.unlink()
        with (
            patch(
                "snap_bot.service.NetworkClient",
                side_effect=AssertionError("Fact source contacted network"),
            ),
            patch("snap_bot.service.UnixHTTPConnection") as connection,
        ):
            response = connection.return_value.getresponse.return_value

            def reply():
                f.content = json.loads(input_path.read_bytes())
                f.digest = f.content["content_hash"]
                response.status = 200
                response.read.return_value = json.dumps(self.fake_render()).encode()
                return response

            connection.return_value.getresponse.side_effect = reply
            connection.return_value.sock.getsockopt.return_value = struct.pack(
                "iII", 99, os.getuid(), os.getgid()
            )
            output = execute(
                f.config, f.settings, "synthetic-gateway-secret-at-least-32", claim=claim
            )
        self.assertEqual(output["state"], "VALIDATING")
        self.assertFalse(output["external_delivery"])
        self.assertEqual(f.content["type"], "did_you_know")
        self.assertEqual(f.content["category"], "animals")
        self.assertEqual(f.repo.stage_result(f.job, "design")["attempt"], 1)
        self.assertTrue(any(event["event"] == "fact.reserved" for event in f.repo.timeline(f.job)))
        with f.repo.connect() as db:
            checks = {
                row["role"]: row["status"]
                for row in db.execute(
                    "SELECT role,status FROM owned_agent_contracts WHERE job_id=? AND role IN ('memory','rotation','proofreading','translation')",
                    (f.job,),
                )
            }
            renders = {
                row["role"]: row["status"]
                for row in db.execute(
                    "SELECT role,status FROM owned_agent_contracts WHERE job_id=? AND role IN ('voice','media_verification','judge')",
                    (f.job,),
                )
            }
        self.assertEqual(
            checks,
            dict.fromkeys(("memory", "rotation", "proofreading", "translation"), "SUCCEEDED"),
            msg="guard:execute-runs-check-agents",
        )
        self.assertEqual(
            renders,
            dict.fromkeys(("voice", "media_verification", "judge"), "SUCCEEDED"),
            msg="guard:execute-runs-render-agents",
        )


@unittest.skipUnless(
    os.environ.get("MADAR_P2_MUTATION") != "1"
    and (ownership.local_profile() or os.environ.get("MADAR_REQUIRE_REAL_RENDER") == "1"),
    "Real FFmpeg required outside isolated guard deletions",
)
class RealFactRenderTests(unittest.TestCase):
    def test_all_drawn_text_has_real_font_glyph_coverage(self):
        profile = fact_profile()
        if profile is None:
            self.fail("CI requires real fonts")
        for name in ("font", "latin_font"):
            self.assertEqual(
                hashlib.sha256(Path(profile[name]).read_bytes()).hexdigest(),
                profile[name + "_sha256"],
            )
        charsets = {
            role: run([profile["fc_query"], "--format=%{charset}", profile[name]], 5).stdout
            for role, name in (("arabic", "font"), ("latin", "latin_font"))
        }
        for fact in load_facts():
            for text, _, _, _, role in fact_labels(make_content(fact, time.time())):
                ensure_glyphs(text, charsets[role])

    def test_real_ffmpeg_long_arabic_fact_db_denied_and_safe_boxes(self):
        fixture = FactRendererTests()
        fixture.setUp()
        f = fixture.fixture
        try:
            f.settings["profile"] = fact_profile()
            if f.settings["profile"] is None:
                self.fail("CI requires real FFmpeg")
            f.settings["profile"]["duration_seconds"] = 8
            f.repo.path.chmod(0)
            try:
                with self.assertRaises(PermissionError):
                    f.repo.path.read_bytes()
                with patch("snap_bot.render.proof", return_value=ownership.ISOLATION):
                    result = render(
                        SimpleNamespace(app="snap-bot-v2"), f.settings, f.job, f.digest, 1
                    )
            finally:
                f.repo.path.chmod(0o600)
            self.assertEqual(result["qa"]["duration"], 8)
            self.assertEqual(result["qa"]["audio"], "generated_ambient")
            self.assertEqual(result["safe_area"]["result"], "PASS")
            self.assertTrue(
                all(
                    b["x"] >= 130 and b["x"] + b["width"] <= 950 and b["y"] + b["height"] <= 1580
                    for b in result["safe_area"]["boxes"]
                )
            )
            # The post-render agents accept a real FFmpeg render, not only synthetic bytes.
            from snap_bot.render_agents import check_media, check_voice

            self.assertEqual(check_voice(result)["decision"], "PASS")
            self.assertEqual(
                check_media(result, f.settings)["boxes"][:1],
                ["ftyp"],
                msg="guard:real-render-media",
            )
            f.rendering()
            f.adopt(result)
            self.assertEqual(
                hashlib.sha256((f.legacy() / "did-you-know.mp4").read_bytes()).hexdigest(),
                result["video_sha256"],
            )
        finally:
            fixture.doCleanups()

    def test_real_ffmpeg_fact_inside_the_approved_frame(self):
        from snap_bot import brand_frame

        fixture = FactRendererTests()
        fixture.setUp()
        f = fixture.fixture
        try:
            f.settings["profile"] = fact_profile()
            if f.settings["profile"] is None:
                self.fail("CI requires real FFmpeg")
            f.settings["profile"]["brand_frame"] = "rawwag"
            with patch("snap_bot.render.proof", return_value=ownership.ISOLATION):
                result = render(SimpleNamespace(app="snap-bot-v2"), f.settings, f.job, f.digest, 1)
            brand = brand_frame.load_brand()
            box = brand["clip_box"]
            self.assertEqual(
                result["brand_frame"]["overlay_sha256"], brand["overlay"]["sha256"], "guard:frame"
            )
            self.assertEqual((result["qa"]["width"], result["qa"]["height"]), (1080, 1920))
            self.assertEqual(result["qa"]["audio"], "generated_ambient")
            self.assertTrue(
                all(
                    box["x"] <= b["x"]
                    and b["x"] + b["width"] <= box["x"] + box["width"]
                    and box["y"] <= b["y"]
                    and b["y"] + b["height"] <= box["y"] + box["height"]
                    for b in result["safe_area"]["boxes"]
                ),
                "guard:framed-safe-boxes",
            )
            from snap_bot.render_agents import check_media

            self.assertEqual(check_media(result, f.settings)["boxes"][:1], ["ftyp"])
            # The frame alone carries the identity and the gold border.
            design = Path(f.settings["spool_root"]) / f.job / "attempts/1/design"
            (filters,) = design.glob("*/filters.txt")
            graph = filters.read_text()
            self.assertNotIn("drawbox=x=80:y=340", graph, "guard:framed-no-card-border")
            drawn = [p.read_text() for p in design.glob("*/text*.txt")]
            self.assertFalse(
                any("rawwag" in t or "خالد" in t for t in drawn), "guard:framed-no-second-identity"
            )
            self.assertIn("هل تعلم", "".join(drawn))
            source = Path(f.settings["input_root"]) / f.job / "attempts/1/input/content.json"
            self.assertEqual(
                len(result["safe_area"]["boxes"]),
                len(fact_labels(json.loads(source.read_text()), f.settings["profile"])) - 2,
            )
        finally:
            fixture.doCleanups()

    def test_unknown_frame_refused_before_render(self):
        fixture = FactRendererTests()
        fixture.setUp()
        f = fixture.fixture
        try:
            f.settings["profile"] = fact_profile()
            if f.settings["profile"] is None:
                self.fail("CI requires real FFmpeg")
            f.settings["profile"]["brand_frame"] = "other"
            with self.assertRaisesRegex(ValueError, "Unknown brand frame"):
                render(SimpleNamespace(app="snap-bot-v2"), f.settings, f.job, f.digest, 1)
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
