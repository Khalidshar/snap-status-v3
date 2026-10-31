"""Lossless, measured pages for Quran and mixed adhkar cards. No delivery or network."""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from studio_core.renderer import ensure_glyphs, run

from . import faith, quran_font, sacred_text
from .did_you_know import load_kind, make_content, validate_content
from .fact_layout import (
    _faith_item,
    page_display_body,
    sacred_body_box,
    sacred_labels,
    stack_sacred,
)

KINDS = {"verse", "adhkar_am", "adhkar_pm"}
MAX_PAGES = 15  # At most 180 seconds with the existing 12-second page profile.


def images(
    content: dict[str, Any], profile: dict[str, Any], directory: Path
) -> tuple[list[str], list[tuple[Path, tuple[int, int]]]]:
    validate_content(content)
    if content.get("kind") not in KINDS:
        raise ValueError("Not a paginated sacred kind")
    font = quran_font.font_for(_faith_item(content))
    charset = run([profile["fc_query"], "--format=%{charset}", str(font)], 5).stdout
    ensure_glyphs(content["body"], charset)
    quoted = page_display_body(content, content["body"]) != content["body"]
    _, _, width, height = sacred_body_box(content)
    labels = sacred_labels(content, profile)
    # Character limits only propose boundaries. Every final page is measured by
    # Pango and the entire header/body/source stack is checked in pixels.
    for limit in range(180, 19, -20):
        parts = sacred_text.pages(content["body"], limit)
        if not 1 <= len(parts) <= MAX_PAGES or " ".join(parts) != content["body"]:
            raise ValueError("Invalid sacred pagination")
        rendered = []
        try:
            for index, part in enumerate(parts):
                image = directory / f"body-{limit}-{index}.png"
                size = sacred_text.render_text_png(
                    part,
                    font,
                    image,
                    pango_view=profile["pango_view"],
                    width_px=width - 120 if quoted else width,
                    max_height_px=height,
                    font_size=profile.get("sacred_font_size", 60),
                    layout_path=image.with_suffix(".layout.json") if quoted else None,
                )
                if quoted:
                    image, size = ornaments(image, size, profile, width, height)
                if size[0] > width:
                    raise ValueError("Sacred text exceeds its card")
                stack_sacred(labels, size[1])
                rendered.append((image, size))
        except ValueError as error:
            if str(error) not in {
                "Sacred text image exceeds maximum height",
                "Sacred text exceeds its card",
            }:
                raise
            continue
        return parts, rendered
    raise ValueError("Sacred text cannot fit without changing its characters")


def ink_boxes(
    image: Path, size: tuple[int, int], line_count: int, profile: dict[str, Any]
) -> list[tuple[int, int, int, int]]:
    """Visible alpha bounds in each uniform Pango line band, in actual pixels."""
    width, height = size
    if not 1 <= line_count <= height or not 0 < width <= 1024 or not 0 < height <= 2048:
        raise ValueError("Invalid sacred image measurement")
    raw = subprocess.run(  # noqa: S603  # fixed FFmpeg argv, no shell or network
        [
            profile["ffmpeg"],
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(image),
            "-vf",
            "alphaextract",
            "-frames:v",
            "1",
            "-threads",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    if len(raw) != width * height:
        raise ValueError("Invalid sacred alpha measurement")
    boxes = []
    for line in range(line_count):
        left, top, right, bottom = width, height, -1, -1
        for y in range(round(line * height / line_count), round((line + 1) * height / line_count)):
            row = raw[y * width : (y + 1) * width]
            for x, alpha in enumerate(row):
                if alpha:
                    left, right = min(left, x), max(right, x)
                    top, bottom = min(top, y), max(bottom, y)
        if right < left:
            raise ValueError("Empty sacred text line")
        boxes.append((left, top, right - left + 1, bottom - top + 1))
    return boxes


def ornaments(
    image: Path, size: tuple[int, int], profile: dict[str, Any], width: int, height: int
) -> tuple[Path, tuple[int, int]]:
    """The KFGQPC font lacks FD3E/FD3F: draw only these decorative glyphs in Naskh.

    The Quran body remains a separate, unmodified KFGQPC image. Neither glyph
    fallback nor substitutions are used for any character of the source text.
    """
    font = quran_font.font_for("hadith")
    ensure_glyphs(
        "\ufd3f\ufd3e", run([profile["fc_query"], "--format=%{charset}", str(font)], 5).stdout
    )
    layout = json.loads(image.with_suffix(".layout.json").read_text())
    lines = ink_boxes(image, size, len(layout["output"]["lines"]), profile)
    first, last = lines[0], lines[-1]
    body_x = (width - size[0]) // 2
    marks = []
    placements = []
    for index, (mark, line) in enumerate(zip(("\ufd3f", "\ufd3e"), (first, last), strict=True)):
        font_size = profile.get("sacred_font_size", 60)
        desired_height = round(line[3] * 0.9)
        # Match visible glyph heights, not em sizes: the two fonts have very
        # different metrics. No scaling or stretching of the glyph image.
        for trial in range(3):
            path = image.with_name(image.stem + f"-bracket-{index}-{trial}.png")
            dims = sacred_text.render_text_png(
                mark,
                font,
                path,
                pango_view=profile["pango_view"],
                width_px=240,
                font_size=font_size,
                max_height_px=height,
            )
            box = ink_boxes(path, dims, 1, profile)[0]
            if trial and 0.75 * line[3] <= box[3] <= line[3]:
                break
            font_size = max(1, round(font_size * desired_height / box[3]))
        else:
            raise ValueError("Decorative bracket cannot match Quran line height")
        left = body_x + line[0] + line[2] + 12 if index == 0 else body_x + line[0] - 12 - box[2]
        top = line[1] + (line[3] - box[3]) // 2
        if left < 0 or left + box[2] > width or top < 0 or top + box[3] > size[1]:
            raise ValueError("Sacred text exceeds its card")
        placements.append((left - box[0], top - box[1]))
        marks.append(path)
    total_height = size[1]
    output = image.with_name(image.stem + "-quoted.png")
    graph = (
        f"[0:v]pad={width}:{total_height}:{body_x}:0:color=black@0[c];"
        f"[c][1:v]overlay=x={placements[0][0]}:y={placements[0][1]}:format=auto[a];"
        f"[a][2:v]overlay=x={placements[1][0]}:y={placements[1][1]}:format=auto[out]"
    )
    run(
        [
            profile["ffmpeg"],
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-filter_complex_threads",
            "1",
            "-i",
            str(image),
            "-i",
            str(marks[0]),
            "-i",
            str(marks[1]),
            "-filter_complex",
            graph,
            "-map",
            "[out]",
            "-frames:v",
            "1",
            "-threads",
            "1",
            str(output),
        ],
        30,
    )
    return output, (width, total_height)


def evidence(content: dict[str, Any], parts: list[str], seconds: float) -> dict[str, Any]:
    font = quran_font.font_for(_faith_item(content))
    ornament_font = (
        quran_font.font_for("hadith")
        if page_display_body(content, content["body"]) != content["body"]
        else None
    )
    return {
        "kind": content["kind"],
        "fact_id": content["fact_id"],
        "pages": parts,
        "page_seconds": seconds,
        "font_sha256": hashlib.sha256(font.read_bytes()).hexdigest(),
        "ornament_font_sha256": hashlib.sha256(ornament_font.read_bytes()).hexdigest()
        if ornament_font
        else None,
    }


def duration(result: dict[str, Any], base_seconds: float) -> float:
    """Independent admission of page provenance and duration by the media agent."""
    record = result.get("sacred_pagination")
    if record is None:
        return base_seconds
    if not isinstance(record, dict) or set(record) != {
        "kind",
        "fact_id",
        "pages",
        "page_seconds",
        "font_sha256",
        "ornament_font_sha256",
    }:
        raise ValueError("Invalid sacred page evidence")
    if record["kind"] not in KINDS or record["page_seconds"] != base_seconds:
        raise ValueError("Unapproved sacred page duration")
    fact = next((f for f in load_kind(record["kind"]) if f["id"] == record["fact_id"]), None)
    if fact is None:
        raise ValueError("Unknown paginated sacred item")
    content = make_content(fact, 0, record["kind"])
    parts = record["pages"]
    if (
        not isinstance(parts, list)
        or not 1 <= len(parts) <= MAX_PAGES
        or any(not isinstance(p, str) or not 1 <= len(p) <= 180 for p in parts)
        or " ".join(parts) != faith.text(_faith_item(content))
        or content["content_hash"] != result.get("content_hash")
        or record != evidence(content, parts, base_seconds)
    ):
        raise ValueError("Sacred page text or font changed")
    return base_seconds * len(parts)
