"""Approved "روّق مع خالد" frame: pinned assets, FFmpeg command shape and a real compose."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from snap_bot import brand_frame as frame

FFMPEG = "/usr/bin/ffmpeg"
REAL = os.path.exists(FFMPEG) and os.path.exists("/usr/bin/ffprobe")


def blocked(call, *args, **kwargs):
    try:
        call(*args, **kwargs)
    except ValueError:
        return True
    return False


class BrandAssetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        # Mirror repo depth so the Cairo paths (../../fonts/cairo) still resolve.
        self.root = Path(temp.name) / "assets/brand/rawwag"
        shutil.copytree(frame.BRAND, self.root)
        shutil.copytree(frame.ROOT / "assets/fonts/cairo", Path(temp.name) / "assets/fonts/cairo")

    def test_repo_brand_is_intact(self):
        brand = frame.load_brand()
        self.assertEqual(frame.png_size(brand["overlay_path"]), (1080, 1920))
        self.assertEqual(brand["clip_box"], {"x": 108, "y": 210, "width": 864, "height": 1536})

    def test_changed_overlay_refused(self):
        path = self.root / "frame_personal.png"
        data = bytearray(path.read_bytes())
        data[-20] ^= 0xFF
        path.write_bytes(bytes(data))
        self.assertTrue(blocked(frame.load_brand, self.root), "guard:overlay-pinned")

    def test_changed_font_template_or_license_refused(self):
        for name in ("ArefRuqaa-Bold.ttf", "frame_personal.html", "OFL-ArefRuqaa.txt"):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                self.assertTrue(blocked(frame.load_brand, self.root), "guard:pinned-" + name)
                path.write_bytes(original)
        self.assertFalse(blocked(frame.load_brand, self.root))

    def test_symlinked_asset_refused(self):
        path = self.root / "frame_personal.png"
        real = self.root / "real.png"
        path.rename(real)
        path.symlink_to(real)
        self.assertTrue(blocked(frame.load_brand, self.root), "guard:no-symlink")

    def test_wrong_size_or_box_refused(self):
        manifest = json.loads((self.root / "manifest.json").read_text())
        manifest["clip_box"]["width"] = 2000
        (self.root / "manifest.json").write_text(json.dumps(manifest))
        self.assertTrue(blocked(frame.load_brand, self.root), "guard:clip-box")
        self.assertTrue(blocked(frame.png_size, self.root / "frame_personal.html"))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.brand = frame.load_brand()

    def test_brand_text_is_never_drawn_by_ffmpeg(self):
        args = frame.command(FFMPEG, Path("in.mp4"), Path("out.mp4"), self.brand, 4.0, True)
        graph = args[args.index("-filter_complex") + 1]
        self.assertNotIn("drawtext", " ".join(args), "guard:no-drawtext")
        self.assertIn(str(self.brand["overlay_path"]), args)
        self.assertIn("overlay=108:210", graph)
        self.assertIn("force_original_aspect_ratio=decrease", graph, "guard:no-stretch")
        self.assertIn("loudnorm=I=-18:TP=-1.5:LRA=11", args)
        for flag in ("-crf", "-maxrate", "-movflags"):
            self.assertIn(flag, args)

    def test_measured_audio_can_pass_through_unchanged(self):
        args = frame.command(
            FFMPEG, Path("in.mp4"), Path("out.mp4"), self.brand, 8.0, True, normalize_audio=False
        )
        self.assertNotIn("-af", args, "guard:keep-measured-audio")
        self.assertIn("1:a:0", args)

    def test_boxes_follow_the_clip_into_the_frame(self):
        box = self.brand["clip_box"]
        whole = frame.framed_box({"x": 0, "y": 0, "width": 1080, "height": 1920}, box)
        self.assertEqual(whole, box, "guard:framed-box")
        inner = frame.framed_box({"x": 130, "y": 200, "width": 820, "height": 100}, box)
        self.assertEqual(inner, {"x": 212, "y": 370, "width": 656, "height": 80})

    def test_silent_clip_gets_a_silent_track(self):
        args = frame.command(FFMPEG, Path("in.mp4"), Path("out.mp4"), self.brand, 3.0, False)
        self.assertIn("anullsrc=r=48000:cl=stereo", args)
        self.assertIn("3:a", args)
        self.assertNotIn("1:a:0", args)

    def test_inputs_refused_before_any_encode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clip = root / "clip.mp4"
            clip.write_bytes(b"x")
            existing = root / "out.mp4"
            existing.write_bytes(b"x")
            self.assertTrue(blocked(frame.compose, clip, existing), "guard:no-overwrite")
            self.assertTrue(blocked(frame.compose, clip, root / "out.mov"), "guard:mp4-only")
            self.assertTrue(blocked(frame.compose, root / "missing.mp4", root / "new.mp4"))
            link = root / "link.mp4"
            link.symlink_to(clip)
            self.assertTrue(blocked(frame.compose, link, root / "new.mp4"), "guard:no-symlink")


@unittest.skipUnless(REAL, "real FFmpeg/ffprobe required")
class RealComposeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def make_clip(self, name, size, audio):
        path = self.root / name
        args = [FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s={size}:d=2:r=30"]
        if audio:
            args += ["-f", "lavfi", "-i", "sine=f=440:d=2", "-c:a", "aac", "-shortest"]
        subprocess.run([*args, "-c:v", "libx264", str(path)], check=True)
        return path

    def pixel_row(self, video, y):
        raw = subprocess.run(
            [FFMPEG, "-v", "error", "-ss", "1", "-i", str(video), "-frames:v", "1",
             "-vf", f"crop=1080:2:0:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            check=True, capture_output=True,
        ).stdout  # fmt: skip
        return [tuple(raw[i : i + 3]) for i in range(0, 1080 * 3, 3)]

    def test_vertical_clip_with_audio(self):
        result = frame.compose(self.make_clip("v.mp4", "576x1024", True), self.root / "out.mp4")
        self.assertEqual(result["source"]["audio"], True)
        self.assertAlmostEqual(result["duration"], 2.0, delta=0.25)
        self.assertFalse((self.root / "out.mp4.partial").exists(), "guard:partial-cleaned")
        row = self.pixel_row(self.root / "out.mp4", 960)
        border = max(row[x][0] for x in range(20, 30))
        background = row[60]
        self.assertGreater(border - background[0], 50, "guard:gold-frame-visible")

    def test_landscape_silent_clip_is_letterboxed(self):
        result = frame.compose(self.make_clip("h.mp4", "1280x720", False), self.root / "o.mp4")
        self.assertEqual(result["source"]["audio"], False)
        top_of_box = self.pixel_row(self.root / "o.mp4", 300)
        # The 16:9 clip sits centred in the 9:16 box: above it is padding, not stretched video.
        self.assertLess(max(top_of_box[540]), 90, "guard:letterbox-not-stretch")


if __name__ == "__main__":
    unittest.main()
