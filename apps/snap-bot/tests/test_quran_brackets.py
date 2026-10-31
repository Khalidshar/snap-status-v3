"""Independent Linux pixel checks for decorative brackets, not sacred text edits."""

import hashlib
import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from test_sacred_render import _alpha_pixels

from snap_bot import faith, quran_font, sacred_cards
from snap_bot.did_you_know import load_kind, make_content

ROOT = Path(__file__).resolve().parents[3]


def bounds(pixels):
    assert pixels, "Expected visible ink"
    xs, ys = zip(*pixels, strict=True)
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def test_bracket_font_is_only_the_pinned_system_amiri():
    profile = tomllib.loads((ROOT / "config/snap/did_you_know.toml").read_text())["slice"][
        "profile"
    ]
    selected = sacred_cards.ornament_font(profile)
    assert selected == sacred_cards.AMIRI_REGULAR
    assert (
        hashlib.sha256(selected.read_bytes()).hexdigest()
        == profile["font_files"]["system:Amiri-Regular"]
    )
    family = subprocess.run(
        [profile["fc_query"], "--format=%{family}", str(selected)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert family == "Amiri"
    fact = load_kind("verse")[0]
    content = make_content(fact, 0, "verse")
    evidence = sacred_cards.evidence(content, [content["body"]], 8)
    assert evidence["font_sha256"] == quran_font.FONT_SHA256
    assert evidence["ornament_font_sha256"] == profile["font_files"]["system:Amiri-Regular"]


@pytest.mark.parametrize("problem", ("missing", "changed_bytes", "changed_pin"))
def test_bracket_font_has_no_unverified_or_naskh_fallback(tmp_path, monkeypatch, problem):
    profile = tomllib.loads((ROOT / "config/snap/did_you_know.toml").read_text())["slice"][
        "profile"
    ]
    font = tmp_path / "Amiri-Regular.ttf"
    if problem != "missing":
        raw = sacred_cards.AMIRI_REGULAR.read_bytes()
        font.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]) if problem == "changed_bytes" else raw)
    if problem == "changed_pin":
        profile["font_files"]["system:Amiri-Regular"] = "0" * 64
    monkeypatch.setattr(sacred_cards, "AMIRI_REGULAR", font)
    with pytest.raises(ValueError):
        sacred_cards.ornament_font(profile)


@pytest.mark.skipif(not shutil.which("pango-view"), reason="Linux Pango pixel measurement")
@pytest.mark.parametrize("sample", ("short", "multiline"))
@pytest.mark.parametrize("font_size", (60, 72))
def test_brackets_touch_first_and_last_lines_in_pixels(tmp_path, sample, font_size):
    profile = tomllib.loads((ROOT / "config/snap/did_you_know.toml").read_text())["slice"][
        "profile"
    ]
    profile.update(pango_view=shutil.which("pango-view"), sacred_font_size=font_size)
    items = sorted(faith.load("verse"), key=lambda item: len(faith.text(item)))
    item = items[0] if sample == "short" else items[-1]
    fact = next(row for row in load_kind("verse") if row["id"] == item["id"])
    value = make_content(fact, 0, "verse")
    original = faith.text(item)
    mark = hashlib.sha256(original.encode()).hexdigest()
    parts, rendered = sacred_cards.images(value, profile, tmp_path)
    assert " ".join(parts) == original and value["body"] == original
    assert hashlib.sha256(" ".join(parts).encode()).hexdigest() == mark
    for quoted, (width, height) in rendered:
        body = quoted.with_name(quoted.name.replace("-quoted.png", ".png"))
        layout = json.loads(body.with_suffix(".layout.json").read_text())
        assert layout["text"] in parts
        count = len(layout["output"]["lines"])
        if sample == "multiline":
            assert count > 1
        body_width = 700
        body_x = (width - body_width) // 2
        raw_body = _alpha_pixels(body.read_bytes())
        raw_quoted = _alpha_pixels(quoted.read_bytes())
        rgba = subprocess.run(
            [
                profile["ffmpeg"],
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(quoted),
                "-frames:v",
                "1",
                "-threads",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgba",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
        assert len(raw_body) == body_width * height
        # The source text's actual alpha pixels survive composition unchanged.
        for y in range(height):
            for x in range(body_width):
                if raw_body[y * body_width + x]:
                    assert raw_quoted[y * width + body_x + x] == raw_body[y * body_width + x]
        for index, line in ((0, 0), (1, count - 1)):
            low, high = round(line * height / count), round((line + 1) * height / count)
            text = bounds(
                [
                    (x + body_x, y)
                    for y in range(low, high)
                    for x in range(body_width)
                    if raw_body[y * body_width + x]
                ]
            )
            bracket = bounds(
                [
                    (x, y)
                    for y in range(low, high)
                    for x in (range(text[2], width) if index == 0 else range(0, text[0]))
                    if raw_quoted[y * width + x]
                ]
            )
            gap = bracket[0] - text[2] if index == 0 else text[0] - bracket[2]
            assert 0 <= gap < 40, "guard:bracket-adjacent-to-ink"
            colors = {
                tuple(rgba[(y * width + x) * 4 : (y * width + x) * 4 + 3])
                for y in range(bracket[1], bracket[3])
                for x in range(bracket[0], bracket[2])
                if rgba[(y * width + x) * 4 + 3] == 255
            }
            assert colors == {(246, 238, 222)}, "Bracket uses the Quran text color"
            text_height, bracket_height = text[3] - text[1], bracket[3] - bracket[1]
            assert 0.75 * text_height <= bracket_height <= text_height
            assert abs((text[1] + text[3]) - (bracket[1] + bracket[3])) <= 2
            assert 0 <= bracket[0] < bracket[2] <= 820 and 0 <= bracket[1] < bracket[3] <= height
            # Both ornaments have their outer tips facing away from the text:
            # the middle hook embraces the body on the inside in RTL.
            middle_y = (bracket[1] + bracket[3]) // 2
            edge_rows = list(range(bracket[1], bracket[1] + max(1, bracket_height // 6))) + list(
                range(bracket[3] - max(1, bracket_height // 6), bracket[3])
            )
            middle = [
                x
                for y in range(middle_y - bracket_height // 10, middle_y + bracket_height // 10 + 1)
                for x in range(bracket[0], bracket[2])
                if raw_quoted[y * width + x]
            ]
            tips = [
                x
                for y in edge_rows
                for x in range(bracket[0], bracket[2])
                if raw_quoted[y * width + x]
            ]
            assert min(middle) < min(tips) if index == 0 else max(middle) > max(tips)
