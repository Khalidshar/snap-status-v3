"""Offline checks for assets imported into the two lenses (no third-party dependencies)."""

import argparse
import json
import struct
import sys
import unicodedata
import zlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 8 * 1024 * 1024
ICON_DOC = "https://developers.snap.com/lens-studio/publishing/configuring/creating-an-icon"
BANNED = ("تابع", "شارك", "أرسل", "qr", "follow", "share")


def strings(value: Any, location: str = "content") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(location, value)]
    if isinstance(value, dict):
        return [
            item for key, child in value.items() for item in strings(child, f"{location}.{key}")
        ]
    if isinstance(value, list):
        return [
            item for i, child in enumerate(value) for item in strings(child, f"{location}[{i}]")
        ]
    return []


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(c for c in value if unicodedata.category(c) not in {"Mn", "Cf"})


def answer_values(content: dict[str, Any]) -> list[bool]:
    answers = [item["a"] for item in content["truefalse"]]
    if any(answer not in {"صح", "خطأ"} for answer in answers):
        raise ValueError("answers must be صح or خطأ")
    return [answer == "صح" for answer in answers]


def answer_line(content: dict[str, Any]) -> str:
    return "isTrueAnswers = [" + ", ".join(str(v).lower() for v in answer_values(content)) + "];"


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid PNG signature")
    offset = 8
    size = None
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("truncated PNG chunk")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        end = offset + 8 + length
        if end + 4 > len(data):
            raise ValueError("truncated PNG payload")
        payload = data[offset + 8 : end]
        crc = struct.unpack(">I", data[end : end + 4])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            raise ValueError("PNG CRC mismatch")
        if size is None:
            if kind != b"IHDR" or length != 13:
                raise ValueError("missing IHDR")
            size = struct.unpack(">II", payload[:8])
            if min(size) <= 0:
                raise ValueError("empty PNG")
        if kind == b"IEND":
            if length or end + 4 != len(data):
                raise ValueError("invalid IEND")
            return size
        offset = end + 4
    raise ValueError("missing IEND")


def audit(root: Path = ROOT) -> tuple[list[str], dict[str, int], str]:
    errors: list[str] = []
    totals: dict[str, int] = {}
    try:
        content = json.loads((root / "content.json").read_text(encoding="utf-8"))
        answers = answer_values(content)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"CONTENT: {exc}"], totals, ""
    items = content["truefalse"]
    if len(items) != 20 or answers != [True] * 10 + [False] * 10:
        errors.append("ANSWERS: expected 20 items, first 10 true and remaining 10 false")
    for location, value in strings(content):
        for word in BANNED:
            if normalized(word) in normalized(value):
                errors.append(f"FORBIDDEN: {location}: {word}")
    pack = root / "pack"
    for prefix in ("q", "a"):
        actual = {p.name for p in (pack / "truefalse").glob(f"{prefix}*.png")}
        expected = {f"{prefix}{i:02}.png" for i in range(1, len(items) + 1)}
        if actual != expected:
            errors.append(
                f"SEQUENCE: {prefix}: missing={sorted(expected - actual)}, "
                f"extra={sorted(actual - expected)}"
            )
    titles = {p.name for p in (pack / "meter").glob("title*.png")}
    expected_titles = {f"title{i:02}.png" for i in range(1, len(content["meter"]) + 1)}
    if not content["meter"] or titles != expected_titles:
        errors.append("TITLES: missing, empty or unexpected meter titles")
    for relative in (
        "brand/icon.png",
        "meter/spinner.png",
        "truefalse/hint-left.png",
        "truefalse/hint-right.png",
    ):
        if not (pack / relative).is_file():
            errors.append(f"MISSING: {relative}")
    emoji = {p.name for p in (pack / "truefalse/emoji").glob("*.png")}
    expected_emoji = {f"{prefix}{i}.png" for prefix in ("correct", "wrong") for i in range(1, 7)}
    if emoji != expected_emoji:
        errors.append("EMOJI: expected six correct and six wrong images")
    dates = {p.name for p in (pack / "camera/datestamp").glob("*.png")}
    if dates != {*(f"d{i}.png" for i in range(10)), "dot.png", "apos.png", "sample.png"}:
        errors.append("DATESTAMP: missing or unexpected date images")
    for path in sorted(pack.rglob("*.png")):
        try:
            dimensions = png_size(path)
            if (
                path.parent == pack / "truefalse"
                and path.stem in {f"{prefix}{i:02}" for prefix in ("q", "a") for i in range(1, 21)}
                and dimensions != (720, 380)
            ):
                errors.append(f"CARD: expected 720x380, got {dimensions}: {path.name}")
            if max(dimensions) > 2048:
                errors.append(f"DIMENSIONS: {path.relative_to(root)}: {dimensions}")
            if (
                path in (pack / "brand/icon.png", pack / "brand/icon-320.png")
                and dimensions != (320, 320)
            ):
                errors.append(f"ICON: expected 320x320, got {dimensions}; {ICON_DOC}")
        except (OSError, ValueError) as exc:
            errors.append(f"PNG: {path.relative_to(root)}: {exc}")
    for lens, script in (("truefalse", "TiltQuiz.js"), ("meter", "RandomSpinner.js")):
        # Shared brand is counted in both import sets; source JSON is counted conservatively.
        files = [
            p for folder in (pack / lens, pack / "brand") for p in folder.rglob("*") if p.is_file()
        ]
        files += [root / "scripts" / script, root / "content.json"]
        try:
            totals[lens] = sum(p.stat().st_size for p in files)
            if totals[lens] > MAX_BYTES:
                errors.append(f"SIZE: {lens}: {totals[lens]} > {MAX_BYTES}")
        except OSError as exc:
            errors.append(f"SIZE: {lens}: {exc}")
    return errors, totals, answer_line(content)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    errors, totals, line = audit(args.root)
    print(line)
    for lens, size in totals.items():
        print(f"{lens}: {size} bytes / {MAX_BYTES}")
    for error in errors:
        print(error, file=sys.stderr)
    print(f"{'FAIL' if errors else 'PASS'}: {len(errors)} error(s)")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
