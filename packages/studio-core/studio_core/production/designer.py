"""Designer agent (owner 3 Nov: «احتاج وكيل مصمم»، «المصمم هو يحدد»).

Decides each clip's look before rendering and inspects the result after. Free and local:
rules plus pixel measurements on the renderer, no paid service and no network.

Before: style (moving footage / footage with translucent cards / solid green), the backdrop
that suits the topic, and how much to dim it for its real brightness.
After: refuses black or grey bars beside the picture or clip.
"""

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from studio_core.presentation import footage

# Words in the clip's text that call for a backdrop, and the words in a footage title that
# provide it. Only the approved, fingerprinted pool is ever chosen.
TOPICS = {
    "sea": (
        ("بحر", "ساحل", "سفن", "سفينة", "ميناء", "جزيرة", "موج", "صيد", "بحري", "خليج", "سياح"),
        ("water", "waves", "sea", "pier"),
    ),
    "sun": (
        ("صباح", "شمس", "نخل", "صحراء", "فجر", "شروق"),
        ("sun", "palm", "hills"),
    ),
    "flowers": (
        ("موضة", "جمال", "أزياء", "ورد", "عطر", "حديقة", "فستان"),
        ("flowers",),
    ),
    "dark": (
        ("ليل", "نوم", "تنام", "ظلام"),
        ("smoke", "black"),
    ),
}
# Mean luma (0-255) the dimmed backdrop should land near so cream and gold text stay readable.
TARGET_LUMA = 70.0


def luma(path: Path) -> float:
    """Mean brightness of three frames of a backdrop, measured on a 32x56 grey thumbnail."""
    values = []
    for second in (0.5, 2.0, 4.0):
        raw = subprocess.run(  # noqa: S603 - fixed tool path, generated arguments
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-ss",
                str(second),
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=32:56,format=gray",
                "-f",
                "rawvideo",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        ).stdout
        if raw:
            values.append(sum(raw) / len(raw))
    if not values:
        raise ValueError("DESIGNER_FOOTAGE_UNREADABLE")
    return sum(values) / len(values)


def decide(p: dict[str, Any], value: dict[str, Any], clips: list[dict[str, Any]]) -> dict[str, Any]:
    items = [s for s in p["scenes"] if s.get("role") not in {"hook", "cta"}]
    pictures = sum(1 for s in items if s.get("image"))
    reasons = []
    if not pictures:
        style = "footage"
        reasons.append("مقطع نصي: خلفية متحركة والنص فوقها مباشرة")
    elif pictures == len(items):
        style = "solid"
        reasons.append("كل البطاقات فيها صور: خلفية خضراء ثابتة والصورة هي البارزة (معتمد)")
    else:
        style = "mix"
        reasons.append("بعض البطاقات بلا صور: خلفية متحركة مع بطاقات شفافة ليبقى المقطع متصلًا")

    kind = value.get("kind") or p["key"]
    pool = [c for c in clips if c["pool"] == footage.pool(kind)] or clips
    text = " ".join(s.get("body", "") + " " + s.get("head", "") for s in items)
    wanted = {topic for topic, (words, _) in TOPICS.items() if any(word in text for word in words)}

    def fits(clip: dict[str, Any]) -> int:
        title = clip["title"].lower()
        return sum(any(word in title for word in TOPICS[topic][1]) for topic in wanted)

    best = max(fits(c) for c in pool)
    candidates = [c for c in pool if fits(c) == best]
    # Same content, same backdrop: a retry never changes the picture behind the text.
    index = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) % len(candidates)
    chosen = candidates[index]
    if best:
        reasons.append(f"الخلفية تناسب الموضوع: {chosen['title']}")
    else:
        reasons.append(f"خلفية هادئة من مجموعة «{footage.pool(kind)}»: {chosen['title']}")
    path = footage.ROOT / chosen["file"]
    brightness = luma(path)
    dim = round(min(0.55, max(0.15, 1 - TARGET_LUMA / max(brightness, 1.0))), 2)
    reasons.append(f"سطوع الخلفية {brightness:.0f}/255، التعتيم {int(dim * 100)}٪")
    return {
        "schema": 1,
        "agent": "designer",
        "style": style,
        "footage": chosen["file"],
        "footage_title": chosen["title"],
        "footage_path": str(path),
        "footage_luma": round(brightness, 1),
        "dim": dim,
        "reasons": reasons,
    }


def frame(video: Path, second: float) -> bytes:
    return subprocess.run(  # noqa: S603 - fixed tool path, generated arguments
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-ss",
            str(second),
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout


def column(raw: bytes, box: dict[str, int], x: int) -> tuple[list[float], int]:
    pixels = []
    for y in range(box["y"] + 8, box["y"] + box["height"] - 8, 8):
        i = 3 * (y * 1080 + x)
        pixels.append(raw[i : i + 3])
    means = [sum(p[c] for p in pixels) / len(pixels) for c in range(3)]
    spread = max(max(p[c] for p in pixels) - min(p[c] for p in pixels) for c in range(3))
    return means, spread


def bar(raw: bytes, box: dict[str, int], x: int, inward: int) -> str | None:
    """Black or grey when a column at the window's edge is flat and colourless while the
    picture further in is not: a bar, not merely a dark backdrop."""
    means, spread = column(raw, box, x)
    if spread > 10:
        return None
    inner, _ = column(raw, box, x + inward)
    if max(abs(a - b) for a, b in zip(means, inner, strict=True)) < 15:
        return None
    if max(means) < 12:
        return "black"
    if max(means) - min(means) < 6 and 30 < sum(means) / 3 < 220:
        return "grey"
    return None


def inspect(video: Path, duration: float, box: dict[str, int]) -> dict[str, Any]:
    """Five moments of the finished video; the 6 px just inside each side of the window."""
    found = []
    for k in range(5):
        second = round(duration * (k + 0.5) / 5, 2)
        raw = frame(video, second)
        if len(raw) != 1080 * 1920 * 3:
            raise ValueError("DESIGNER_FRAME_SIZE")
        for side, x, inward in (
            ("left", box["x"] + 6, 80),
            ("right", box["x"] + box["width"] - 7, -80),
        ):
            colour = bar(raw, box, x, inward)
            if colour:
                found.append({"second": second, "side": side, "colour": colour})
    if found:
        raise ValueError("DESIGNER_REJECTED_BARS: " + ",".join(f["colour"] for f in found))
    return {"moments": 5, "bars": []}
