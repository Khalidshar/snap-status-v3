"""Bounded fact-card labels; FFmpeg measures shaped text before video creation."""

import re
import subprocess
import textwrap
from collections.abc import Mapping, Sequence
from typing import Any

from .did_you_know import is_sacred, validate_content

SAFE = (130, 170, 950, 1580)
# The sacred body image: centred between the title and the source line.
SACRED_BODY = (130, 560, 820, 720)


def drawing_text(text: str) -> str:
    # drawtext/FriBidi does not reliably keep Arabic combining marks attached.
    # Remove vowel marks and shadda from display only, never stored content.
    visible = "".join(
        char for char in text if not (0x064B <= ord(char) <= 0x0655 or ord(char) == 0x0670)
    )
    return re.sub(r"\s+([،؛؟])", r"\1", visible)


def display_text(text: str, role: str) -> str:
    # drawtext's FriBidi paragraph defaults to LTR. Renderer-owned embedding
    # sets Arabic paragraph direction while preserving Latin/numeric runs.
    # Catalog validation and visible-glyph checks happen before this wrapper.
    return ("\u202b" if role == "arabic" else "\u202a") + text + "\u202c"


def fact_labels(
    content: dict[str, Any], profile: Mapping[str, Any] | None = None
) -> list[tuple[str, int, int, str, str]]:
    validate_content(content)
    if is_sacred(content):
        raise ValueError("Sacred text is drawn whole, never wrapped or stripped")
    profile = profile or {}
    line_width = profile.get("fact_line_width", 26)
    if line_width not in (20, 26):
        raise ValueError("Unsupported fact line width")
    lines = textwrap.wrap(
        content["body"], width=line_width, break_long_words=False, break_on_hyphens=False
    )
    if not 1 <= len(lines) <= 5 or any(len(line) > line_width for line in lines):
        raise ValueError("Fact exceeds safe line limits")
    profile = profile or {}
    body_size = profile.get("fact_body_font_size", 68)
    title_size = profile.get("fact_title_font_size", 96)
    if body_size not in (68, 80) or title_size not in (96, 104):
        raise ValueError("Unsupported fact font size")
    block_height = (len(lines) - 1) * 115 + body_size
    first_y = round((600 + 1260 - block_height) / 2)
    labels = [
        ("روّق مع خالد", 50, 210, "0xD6B26C", "arabic"),
        ("@rawwag_khalid", 38, 290, "0xD6B26C", "latin"),
        (content["title"], title_size, 430, "0xD6B26C", "arabic"),
    ]
    labels += [
        (line, body_size, first_y + 115 * i, "0xF6EEDE", "arabic") for i, line in enumerate(lines)
    ]
    labels += [
        (content["attribution"], 40, 1320, "0xF6EEDE", "arabic"),
        ("تابعني للمزيد", 56, 1480, "0xF6EEDE", "arabic"),
    ]
    return [(drawing_text(text), size, y, color, role) for text, size, y, color, role in labels]


def sacred_labels(
    content: dict[str, Any], profile: Mapping[str, Any] | None = None
) -> list[tuple[str, int, int, str, str]]:
    """The fact card without body lines: the body is drawn whole, as one shaped image."""
    if not is_sacred(content):
        raise ValueError("Not sacred content")
    validate_content(content)
    title_size = (profile or {}).get("fact_title_font_size", 96)
    if title_size not in (96, 104):
        raise ValueError("Unsupported fact font size")
    # Title and source carry no vowel marks, so drawtext shapes them safely.
    labels = [
        ("روّق مع خالد", 50, 210, "0xD6B26C", "arabic"),
        ("@rawwag_khalid", 38, 290, "0xD6B26C", "latin"),
        (content["title"], title_size, 430, "0xD6B26C", "arabic"),
        (content["attribution"], 40, 1320, "0xF6EEDE", "arabic"),
        ("تابعني للمزيد", 56, 1480, "0xF6EEDE", "arabic"),
    ]
    return [(drawing_text(text), size, y, color, role) for text, size, y, color, role in labels]


def label_y(label: Sequence[Any]) -> str:
    # Equal baseline grid, independent of each shaped line's ascent/descenders.
    return f"{label[2] + label[1]}-ascent"


def card_filters(fps: int = 24) -> list[str]:
    # Exact RGB endpoint interpolation; the identity uses approved fonts only.
    if type(fps) is not int or not 1 <= fps <= 60:
        raise ValueError("Invalid gradient frame rate")
    return [
        "format=gbrp",
        "geq=r='11+(6-11)*Y/(H-1)':g='61+(42-61)*Y/(H-1)':b='46+(32-46)*Y/(H-1)'",
        "format=yuv420p",
        "loop=loop=-1:size=1:start=0",
        f"setpts=N/({fps}*TB)",
        "drawbox=x=80:y=340:w=920:h=1240:color=0xD6B26C:t=2",
    ]


def measured_box(
    pixels: bytes, canvas_width: int = 1080, canvas_height: int = 1920
) -> dict[str, int]:
    if len(pixels) != canvas_width * canvas_height:
        raise ValueError("Missing shaped-text measurement")
    x = canvas_width
    y = canvas_height
    rightmost = -1
    bottommost = -1
    # Black is 0 in the RGB → gray probe. A threshold also excludes any
    # limited-range black level and negligible antialias fringes.
    for position, value in enumerate(pixels):
        if value <= 32:
            continue
        py, px = divmod(position, canvas_width)
        x = min(x, px)
        y = min(y, py)
        rightmost = max(rightmost, px)
        bottommost = max(bottommost, py)
    width = rightmost - x + 1
    height = bottommost - y + 1
    left, top, right, bottom = SAFE
    if width <= 0 or height <= 0 or x < left or y < top or x + width > right or y + height > bottom:
        raise ValueError(f"Text exceeds safe area: x={x} y={y} width={width} height={height}")
    return {"x": x, "y": y, "width": width, "height": height}


def measure_label(profile: Mapping[str, Any], filter_value: str) -> dict[str, int]:
    # One label on a black canvas, measured from actual pixels, never logs.
    args = [
        profile["ffmpeg"],
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=1080x1920:r=1:d=1",
        "-vf",
        "format=rgb24," + filter_value + ",format=gray",
        "-frames:v",
        "1",
        "-threads",
        "1",
        "-pix_fmt",
        "gray",
        "-f",
        "rawvideo",
        "-",
    ]
    pixels = subprocess.run(args, check=True, capture_output=True, timeout=30).stdout  # noqa: S603  # fixed argv, no shell
    return measured_box(pixels)
