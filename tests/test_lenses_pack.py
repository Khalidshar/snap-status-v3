"""Run with pytest, or: python -m unittest discover -s tests -p test_lenses_pack.py."""

import importlib.util
import json
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "lenses"
SPEC = importlib.util.spec_from_file_location("lenses_check", ROOT / "tools" / "check.py")
assert SPEC and SPEC.loader
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x00" * width * 4) * height))
        + chunk(b"IEND", b"")
    )


class LensesPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "lenses"
        shutil.copytree(ROOT, self.root)
        # A synthetic valid fixture changes no tracked source content.
        content_path = self.root / "content.json"
        self.content = json.loads(content_path.read_text(encoding="utf-8"))
        self.content["truefalse"][16]["q"] = "سؤال تجريبي"
        content_path.write_text(json.dumps(self.content), encoding="utf-8")
        (self.root / "pack/brand/icon.png").write_bytes(png(320, 320))

    def test_valid_fixture_and_answer_line(self) -> None:
        errors, totals, line = CHECK.audit(self.root)
        self.assertEqual(errors, [])
        self.assertEqual(CHECK.answer_values(self.content), [True] * 10 + [False] * 10)
        self.assertEqual(line.count("true"), 10)
        self.assertEqual(line.count("false"), 10)
        self.assertTrue(all(value < CHECK.MAX_BYTES for value in totals.values()))

    def test_repository_pack_is_ready(self) -> None:
        errors, _, _ = CHECK.audit(ROOT)
        self.assertEqual(errors, [], "\n".join(errors))

    def test_missing_and_extra_question_and_answer(self) -> None:
        (self.root / "pack/truefalse/q01.png").rename(self.root / "pack/truefalse/q21.png")
        (self.root / "pack/truefalse/a02.png").unlink()
        errors, _, _ = CHECK.audit(self.root)
        self.assertEqual(sum(e.startswith("SEQUENCE:") for e in errors), 2)

    def test_all_forbidden_words_and_normalization(self) -> None:
        for word in ("تابع", "شارك", "أرسل", "QR", "follow", "SHARE", "تَابع", "f\u200bollow"):
            self.content["meter"][0] = word
            (self.root / "content.json").write_text(json.dumps(self.content), encoding="utf-8")
            errors, _, _ = CHECK.audit(self.root)
            self.assertTrue(any(e.startswith("FORBIDDEN:") for e in errors), word)

    def test_dimensions_icon_and_corrupt_png(self) -> None:
        (self.root / "pack/brand/icon.png").write_bytes(png(512, 512))
        (self.root / "pack/meter/title01.png").write_bytes(png(2049, 1))
        (self.root / "pack/truefalse/q01.png").write_bytes(b"bad")
        errors, _, _ = CHECK.audit(self.root)
        for prefix in ("ICON:", "DIMENSIONS:", "PNG:"):
            self.assertTrue(any(e.startswith(prefix) for e in errors), errors)

    def test_budget_includes_shared_assets_and_script(self) -> None:
        (self.root / "pack/brand/large.bin").write_bytes(b"0" * CHECK.MAX_BYTES)
        errors, totals, _ = CHECK.audit(self.root)
        self.assertEqual(sum(e.startswith("SIZE:") for e in errors), 2)
        self.assertTrue(all(value > CHECK.MAX_BYTES for value in totals.values()))

    def test_wrong_answer_order_and_invalid_answer(self) -> None:
        self.content["truefalse"][0]["a"] = "خطأ"
        (self.root / "content.json").write_text(json.dumps(self.content), encoding="utf-8")
        self.assertTrue(any(e.startswith("ANSWERS:") for e in CHECK.audit(self.root)[0]))
        self.content["truefalse"][0]["a"] = "unknown"
        (self.root / "content.json").write_text(json.dumps(self.content), encoding="utf-8")
        self.assertTrue(CHECK.audit(self.root)[0][0].startswith("CONTENT:"))

    def test_missing_hint_and_title(self) -> None:
        (self.root / "pack/truefalse/hint-left.png").unlink()
        (self.root / "pack/meter/title01.png").unlink()
        errors, _, _ = CHECK.audit(self.root)
        self.assertTrue(any(e.startswith("MISSING:") for e in errors))
        self.assertTrue(any(e.startswith("TITLES:") for e in errors))


if __name__ == "__main__":
    unittest.main()
