import copy
import hashlib
import json
import os
import shutil
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from snap_bot import font_admission as fonts
from snap_bot.fact_layout import display_text, drawing_text
from snap_bot.render import ensure_glyphs

ROOT = Path(__file__).parents[3]


def profile():
    value = tomllib.loads((ROOT / "config/snap/did_you_know.toml").read_text())["slice"]["profile"]
    return copy.deepcopy(value)


def blocked(call):
    try:
        call()
    except (ValueError, OSError):
        return True
    return False


class LicensedFontTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "assets/fonts", self.root / "assets/fonts")
        self.profile = profile()
        self.profile["font_manifest"] = str(self.root / "assets/fonts/manifest.json")

    def test_default_is_pinned_kufi_bold_and_fallbacks_admitted(self):
        p = profile()
        primary, _latin = fonts.font_paths(p)
        self.assertEqual(primary.name, "NotoKufiArabic-Bold.ttf", "guard:kufi-bold-default")
        self.assertEqual(p["fact_body_font_size"], 68, "guard:kufi-measured-size")
        from snap_bot.did_you_know import load_facts, make_content
        from snap_bot.fact_layout import fact_labels

        for fact in load_facts():
            try:
                labels = fact_labels(make_content(fact, time.time()), p)
            except ValueError as error:
                self.fail("guard:kufi-line-width: approved catalog rejected " + str(error))
            self.assertTrue(
                all(len(label[0]) <= 20 for label in labels[3:-2]), "guard:kufi-line-width"
            )
        # OS/2 usWeightClass is a binary field, not a file-name assumption.
        import struct

        data = primary.read_bytes()
        count = struct.unpack_from(">H", data, 4)[0]
        records = [struct.unpack_from(">4sIII", data, 12 + i * 16) for i in range(count)]
        offset = next(r[2] for r in records if r[0] == b"OS/2")
        self.assertEqual(
            struct.unpack_from(">H", data, offset + 4)[0], 700, "guard:kufi-bold-weight"
        )
        self.assertEqual(
            [p.name for p in fonts.fallback_fonts(p)],
            ["NotoNaskhArabic-Regular.ttf", "Cairo-Regular.ttf", "ae_AlMohanad.ttf"],
        )

    def test_unknown_registry_is_rejected_even_with_self_reported_hash(self):
        manifest = self.root / "assets/fonts/manifest.json"
        raw = manifest.read_bytes() + b" "
        manifest.write_bytes(raw)
        self.assertTrue(blocked(lambda: fonts.font_paths(self.profile)), "guard:registry-pin")

    def test_unknown_font_is_rejected_even_when_profile_hash_matches(self):
        path = self.root / "unknown.ttf"
        path.write_bytes(b"unapproved bytes")
        self.profile["font"] = str(path)
        self.profile["font_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertTrue(blocked(lambda: fonts.font_paths(self.profile)), "guard:unknown-font")

    def test_modified_font_bytes_are_rejected(self):
        target = self.root / self.profile["font"]
        original = target.read_bytes()
        target.write_bytes(original + b"tampered")
        self.assertTrue(blocked(lambda: fonts.font_paths(self.profile)), "guard:font-byte-pin")
        target.write_bytes(original)
        self.profile["font_sha256"] = self.profile["latin_font_sha256"]
        self.assertTrue(blocked(lambda: fonts.font_paths(self.profile)), "guard:font-byte-pin")

    def test_missing_or_tampered_license_is_rejected(self):
        license = self.root / "assets/fonts/notokufiarabic/OFL.txt"
        original = license.read_bytes()
        license.write_bytes(original + b"tampered")
        self.assertTrue(blocked(lambda: fonts.font_paths(self.profile)), "guard:license-byte-pin")
        license.unlink()
        self.assertTrue(blocked(lambda: fonts.font_paths(self.profile)), "guard:license-presence")

    def test_registry_entry_without_license_is_rejected(self):
        path = self.root / "assets/fonts/manifest.json"
        value = json.loads(path.read_bytes())
        selected = next(
            e for e in value["fonts"] if e["file"] == "notokufiarabic/NotoKufiArabic-Bold.ttf"
        )
        selected["licenses"] = []
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        self.profile["font_manifest_sha256"] = digest
        with patch.object(fonts, "MANIFEST_SHA256", digest):
            self.assertTrue(
                blocked(lambda: fonts.font_paths(self.profile)), "guard:license-required"
            )

    def test_profile_inventory_cannot_add_unreviewed_hash(self):
        self.profile["font_files"]["unknown.ttf"] = "0" * 64
        self.assertTrue(blocked(lambda: fonts.font_paths(self.profile)), "guard:profile-inventory")

    def test_fallback_is_explicit_ordered_and_rejects_uncovered_text(self):
        from pathlib import Path

        first, second, third = map(Path, ("first", "second", "third"))
        charsets = {first: "20-7e", second: "20-7e 600-6ff", third: "20-7e 600-6ff"}
        self.assertEqual(
            fonts.choose_font("هل تعلم؟", first, charsets, [second, third], ensure_glyphs),
            second,
            "guard:fallback-order",
        )
        self.assertTrue(
            blocked(
                lambda: fonts.choose_font("👻", first, charsets, [second, third], ensure_glyphs)
            ),
            "guard:fallback-coverage",
        )

    def test_display_marks_and_punctuation_never_change_source(self):
        source = "يومًا ، تتكوّن؟ ١٢٣ IPv6 128"
        before = source
        self.assertEqual(
            drawing_text(source), "يوما، تتكون؟ ١٢٣ IPv6 128", "guard:display-arabic-punctuation"
        )
        self.assertEqual(source, before)
        self.assertEqual(
            display_text("IPv6 128 IPv4", "arabic"),
            "\u202bIPv6 128 IPv4\u202c",
            "guard:mixed-direction",
        )

    def test_all_bundled_fonts_have_license_and_correct_hash(self):
        p = profile()
        root, value = fonts.registry(p)
        for entry in value["fonts"]:
            if entry.get("external_package"):
                continue
            path = root / entry["file"]
            actual = fonts.admit_font(path, entry["sha256"], p)
            self.assertEqual(actual, path)
        self.assertFalse(list(root.rglob("AL-Mohanad-Bold-1.ttf")))
        self.assertFalse(list(root.rglob("al-mohanad.ttf")))


@unittest.skipUnless(
    os.name == "posix" and (os.environ.get("MADAR_FONT_MUTATION") != "1"),
    "Linux real FFmpeg font acceptance",
)
class LicensedFontRealTests(unittest.TestCase):
    def test_all_catalog_labels_fit_production_kufi_safe_area(self):
        from test_did_you_know_render import fact_profile

        from snap_bot.did_you_know import load_facts, make_content
        from snap_bot.fact_layout import fact_labels, label_y, measure_label

        p = fact_profile()
        self.assertIsNotNone(p)
        primary, latin = fonts.font_paths(p)
        seen = set()
        with tempfile.TemporaryDirectory() as directory:
            text_path = Path(directory) / "label.txt"
            for fact in load_facts():
                for text, size, y, color, role in fact_labels(make_content(fact, time.time()), p):
                    key = (text, size, y, role)
                    if key in seen:
                        continue
                    seen.add(key)
                    text_path.write_text(display_text(text, role), encoding="utf-8")
                    font = primary if role == "arabic" else latin
                    value = f"drawtext=fontfile={font}:textfile={text_path}:expansion=none:text_shaping=1:fontsize={size}:fontcolor=white:x=(w-text_w)/2:y={label_y((text, size, y, color, role))}"
                    with self.subTest(fact=fact["id"], label=text):
                        box = measure_label(p, value)
                        self.assertLessEqual(box["x"] + box["width"], 950, "guard:all-catalog-safe")

    def test_real_short_and_mixed_videos_use_approved_fonts_without_delivery(self):
        from types import SimpleNamespace

        from test_did_you_know_render import FactRendererTests, fact_profile
        from test_render_ownership import ISOLATION

        from snap_bot.did_you_know import load_facts, make_content
        from snap_bot.render import render, run

        for identity in ("dyk-01", "dyk-09"):
            fixture = FactRendererTests()
            fixture.setUp()
            try:
                fact = next(f for f in load_facts() if f["id"] == identity)
                fixture.set_content(make_content(fact, time.time()))
                f = fixture.fixture
                p = fact_profile()
                self.assertIsNotNone(p, "CI requires real FFmpeg fonts")
                p["duration_seconds"] = 8
                f.settings["profile"] = p
                charsets = {
                    font: run([p["fc_query"], "--format=%{charset}", str(font)], 5).stdout
                    for font in fonts.font_paths(p)
                }
                for text in (
                    "هل تعلم؟",
                    "يومًا، تتكوّن أرضيًا",
                    "0123456789 ٠١٢٣٤٥٦٧٨٩ IPv6 128 IPv4",
                ):
                    ensure_glyphs(text, charsets[fonts.font_paths(p)[0]])
                with patch("snap_bot.render.proof", return_value=ISOLATION):
                    result = render(
                        SimpleNamespace(app="snap-bot-v2"), f.settings, f.job, f.digest, 1
                    )
                self.assertEqual(result["qa"]["duration"], 8)
                self.assertEqual(result["safe_area"]["result"], "PASS")
                self.assertEqual(result["font_sha256"], p["font_sha256"])
                self.assertTrue((f.spool() / "output/preview.png").exists())
                with f.repo.connect() as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT COUNT(*) FROM deliveries WHERE job_id=?", (f.job,)
                        ).fetchone()[0],
                        0,
                    )
            finally:
                fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
