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
# A hadith card has no title: it opens with its narrator and the Prophet ﷺ, above the
# verbatim text.
PROPHET_LINE = "قال رسول الله ﷺ:"
HADITH_BODY = (130, 620, 820, 660)


def sacred_body_box(content: dict[str, Any]) -> tuple[int, int, int, int]:
    return HADITH_BODY if content.get("kind") in ("hadith", "fast") else SACRED_BODY


# Hadith cards follow the usual Saudi publishing style (الدرر السنية, binbaz.org.sa):
# «عن أبي هريرة رضي الله عنه قال: قال رسول الله ﷺ: «...»» and «رواه البخاري (6018)».
def _faith_item(content: dict[str, Any]) -> dict[str, Any]:
    from . import faith

    item = next((i for i in faith.load(content["kind"]) if i["id"] == content["fact_id"]), None)
    if item is None:
        raise ValueError("Unknown faith item")
    return item


def _hadith_item(content: dict[str, Any]) -> dict[str, Any]:
    return _faith_item(content)


def _after_an(narrator: str) -> str:
    # After «عن» the name is genitive: أبو هريرة → عن أبي هريرة.
    return "أبي " + narrator.removeprefix("أبو ") if narrator.startswith("أبو ") else narrator


def _asked(content: dict[str, Any]) -> bool:
    """The text opens «سُئِلَ ... فقال»: it continues «أن رسول الله ﷺ», not «قال رسول الله ﷺ:»."""
    return str(content["body"]).startswith("سُئِلَ")


def narrator_line(content: dict[str, Any]) -> str:
    """«عن أبي هريرة رضي الله عنه قال:», from the reviewed faith catalog, never from content."""
    item = _faith_item(content)
    line = "عن " + _after_an(str(item["narrator"]))
    if content["kind"] == "fast" and item["speaker"] == "prophet" and _asked(content):
        return line
    return line + (" قالت:" if str(item["narrator"]).endswith("عنها") else " قال:")


def fast_header(content: dict[str, Any]) -> list[tuple[str, int, int, str, str]]:
    """Fasting cards open like a hadith; a Companion's own words get no Prophet ﷺ line."""
    # 36 px keeps the longest fasting narrator (قتادة بن ملحان القيسي) inside SAFE.
    header = [(narrator_line(content), 36, 410, "0xF6EEDE", "arabic")]
    if _faith_item(content)["speaker"] == "prophet":
        line = "أن رسول الله ﷺ" if _asked(content) else PROPHET_LINE
        header.append((line, 76, 490, "0xD6B26C", "arabic"))
    return header


def narration_line(content: dict[str, Any]) -> str:
    """«صحيح البخاري • حديث 6018» → «رواه البخاري (6018)»."""
    source = str(_hadith_item(content)["source"])
    match = re.fullmatch(r"صحيح (\S+) • حديث ([0-9]+)", source)
    if not match:
        raise ValueError("Unknown hadith source format")
    return f"رواه {match[1]} ({match[2]})"


def fast_narration_line(content: dict[str, Any]) -> str:
    """«صحيح مسلم، ١١٦٢» → «رواه مسلم (١١٦٢)»; «سنن أبي داود، ٢٤٤٩» → «رواه أبو داود (٢٤٤٩)»."""
    from . import faith

    item = next((i for i in faith.load("fast") if i["id"] == content["fact_id"]), None)
    if item is None:
        raise ValueError("Unknown fasting reminder")
    match = re.fullmatch(r"(?:صحيح|سنن) (.+)، ([٠-٩]+)", str(item["source"]))
    if not match:
        raise ValueError("Unknown fasting source format")
    # The collector is the subject of «رواه»: أبي داود → أبو داود.
    collector = re.sub(r"^أبي ", "أبو ", match[1])
    return f"رواه {collector} ({match[2]})"


def sacred_display_body(content: dict[str, Any]) -> str:
    """The verbatim body; a hadith is quoted in «», outside the unchanged text."""
    return "«" + content["body"] + "»" if content["kind"] == "hadith" else str(content["body"])


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


# The same identity rows as fact_labels, which keeps its own reviewed copy.
SACRED_IDENTITY = (
    ("روّق مع خالد", 50, 210, "0xD6B26C", "arabic"),
    ("@rawwag_khalid", 38, 290, "0xD6B26C", "latin"),
)


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
        *SACRED_IDENTITY,
        *(
            [
                # 42 px keeps the longest narrator (عمر بن الخطاب) inside SAFE; 52 px did not.
                (narrator_line(content), 42, 405, "0xF6EEDE", "arabic"),
                (PROPHET_LINE, 76, 490, "0xD6B26C", "arabic"),
            ]
            if content["kind"] == "hadith"
            else fast_header(content)
            if content["kind"] == "fast"
            else [(content["title"], title_size, 430, "0xD6B26C", "arabic")]
        ),
        (
            narration_line(content)
            if content["kind"] == "hadith"
            else fast_narration_line(content)
            if content["kind"] == "fast"
            else content["attribution"],
            44 if content["kind"] in ("hadith", "fast") else 40,
            1320,
            "0xF6EEDE",
            "arabic",
        ),
        ("تابعني للمزيد", 56, 1480, "0xF6EEDE", "arabic"),
    ]
    return [(drawing_text(text), size, y, color, role) for text, size, y, color, role in labels]


# Sacred cards stack their lines with fixed gaps and centre the whole block inside the
# frame, instead of fixed rows that leave tall empty bands around a short text.
CARD_TOP, CARD_BOTTOM = 360, 1560
HEADER_GAP, BODY_GAP, FOOTER_GAP = 22, 56, 64


def line_height(size: int) -> int:
    return round(size * 1.35)


def stack_sacred(
    labels: list[tuple[str, int, int, str, str]], body_height: int
) -> tuple[list[tuple[str, int, int, str, str]], int]:
    """Re-place the header, body, source and follow lines as one centred block.

    labels are sacred_labels(): identity (2), header lines, source, follow. Returns the
    labels with new rows and the body's top row.
    """
    identity, header, source, follow = labels[:2], labels[2:-2], labels[-2], labels[-1]
    if not header or not 0 < body_height <= CARD_BOTTOM - CARD_TOP:
        raise ValueError("Invalid sacred layout")
    heights = [line_height(label[1]) for label in header]
    total = (
        sum(heights)
        + HEADER_GAP * (len(header) - 1)
        + BODY_GAP
        + body_height
        + FOOTER_GAP
        + line_height(source[1])
        + FOOTER_GAP
        + line_height(follow[1])
    )
    y = max(CARD_TOP, (CARD_TOP + CARD_BOTTOM - total) // 2)
    if y + total > CARD_BOTTOM:
        raise ValueError("Sacred text exceeds its card")
    placed = list(identity)
    for label, height in zip(header, heights, strict=True):
        placed.append((label[0], label[1], y, label[3], label[4]))
        y += height + HEADER_GAP
    body_top = y - HEADER_GAP + BODY_GAP
    y = body_top + body_height + FOOTER_GAP
    placed.append((source[0], source[1], y, source[3], source[4]))
    y += line_height(source[1]) + FOOTER_GAP
    placed.append((follow[0], follow[1], y, follow[3], follow[4]))
    return placed, body_top


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
