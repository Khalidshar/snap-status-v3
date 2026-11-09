"""Designer agent (owner 3 Nov: «احتاج وكيل مصمم»، «المصمم هو يحدد»).

Decides each clip's look before rendering and inspects the result after. Free and local:
rules plus pixel measurements on the renderer, no paid service and no network.

Before: style (moving footage / footage with translucent cards / solid green), the backdrop
that suits the topic, and how much to dim it for its real brightness.
After: refuses black or grey bars beside the picture or clip.
"""

import hashlib
import re
import sqlite3
import subprocess
from contextlib import closing
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
# Arabic names for the approved backdrops and pools: everything the owner reads is Arabic.
NAMES = {
    "mixkit-1164.mp4": "أمواج في الماء",
    "mixkit-1168.mp4": "ورد يتمايل مع النسيم",
    "mixkit-1170.mp4": "شمس فوق النخيل",
    "mixkit-1183.mp4": "شمس فوق التلال",
    "mixkit-1197.mp4": "رصيف خشبي على الماء",
    "mixkit-1968.mp4": "دخان على خلفية سوداء",
    "mixkit-51500.mp4": "أمواج فيروزية من الأعلى",
    "mixkit-51506.mp4": "أمواج تتكسر على صخرة",
}
LIVE_NAMES = {"calm": "طبيعة هادئة", "night": "طبيعة ليلية", "day": "منظر طبيعي"}
POOLS = {"calm": "الهادئة", "night": "الليلية", "day": "النهارية"}
DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
# Mean luma (0-255) the dimmed backdrop should land near so cream and gold text stay readable.
TARGET_LUMA = 70.0
# Owner decision 8 Nov 2026: the designer also owns contrast where the text sits. The
# brightest tenth of the text band should land near this level, up to 65% dimming.
TARGET_SPOTS = 120.0
MAX_DIM = 0.65
# The approved type (owner: the designer decides font and size). Base body size by length.
FONT = "body"  # المهند، الخط المعتمد
SHORT, LONG = 60, 160
SIZES = {"short": 84, "normal": 72, "long": 72}
# Places and bodies that deserve the eye first; at most two highlighted words per card.
KEY_WORDS = (
    "السعودية",
    "الرياض",
    "جدة",
    "مكة",
    "المدينة",
    "الدمام",
    "الخبر",
    "نيوم",
    "العلا",
    "أبها",
    "تبوك",
    "القصيم",
    "الأحساء",
    "الطائف",
    "الهلال",
    "النصر",
    "الاتحاد",
    "الأهلي",
    "الشباب",
    "أرامكو",
    "تاسي",
    "الخليج",
    "الإمارات",
    "الكويت",
    "قطر",
    "البحرين",
    "عمان",
    "مصر",
    "الأردن",
    "أمريكا",
    "الصين",
    "اليابان",
    "أوروبا",
    "بريطانيا",
    "فرنسا",
    "ألمانيا",
    "آبل",
    "جوجل",
    "مايكروسوفت",
    "سامسونج",
    "تسلا",
    "ميتا",
    "إنفيديا",
    "الذهب",
    "النفط",
    "بيتكوين",
    "رؤية",
)
MAX_KEYS = 2


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


def bright_spots(path: Path) -> float:
    """Brightest tenth (90th percentile) of the band where cards sit, over three frames."""
    values: list[int] = []
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
                # The text band: the middle 70% of the height, full width.
                "-vf",
                "crop=iw:ih*0.7:0:ih*0.15,scale=32:40,format=gray",
                "-f",
                "rawvideo",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        ).stdout
        values += list(raw)
    if not values:
        raise ValueError("DESIGNER_FOOTAGE_UNREADABLE")
    values.sort()
    return float(values[int(len(values) * 0.9)])


def typography(p: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Font, base size, two-page permission and highlighted words for every card.

    Display only: the renderer never changes a letter. Sacred clips keep the approved
    sizes with no highlight and no splitting rules of this agent.
    """
    sacred = p.get("effects") is False
    cards: list[dict[str, Any]] = []
    counts = {"short": 0, "normal": 0, "long": 0}
    keyed = 0
    for scene in p["scenes"]:
        body = scene.get("body", "")
        if sacred or scene.get("verse") or scene.get("role") in {"hook", "cta"}:
            cards.append({})
            continue
        size = "short" if len(body) <= SHORT else "long" if len(body) > LONG else "normal"
        counts[size] += 1
        keys = [m.strip("«»") for m in re.findall(r"«[^»]{2,30}»", body)]
        for word in KEY_WORDS:
            if re.search(r"(?<![\w])(?:ال|و|ب|ل|في)?" + re.escape(word) + r"(?![\w])", body):
                keys.append(word)
        keys = list(dict.fromkeys(keys))[:MAX_KEYS]
        keyed += bool(keys)
        cards.append({"size": SIZES[size], "pages": 2 if size == "long" else 1, "keys": keys})
    reasons = []
    if not sacred:
        reasons.append(
            f"الخط المعتمد (المهند)؛ نص قصير {counts['short']} بحجم كبير، "
            f"عادي {counts['normal']}، طويل {counts['long']} يتوزع على صفحتين بدل القص؛ "
            f"إبراز كلمة مهمة في {keyed} بطاقة".translate(DIGITS)
        )
    return {"font": FONT, "cards": cards}, reasons


def choose_background(
    ranked: list[dict[str, Any]], text: str, history: tuple[Path, str] | None
) -> tuple[dict[str, Any], bool]:
    def pick(clips: list[dict[str, Any]]) -> dict[str, Any]:
        best = [clip for clip in clips if clip["fit"] == clips[0]["fit"]]
        return best[int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) % len(best)]

    if history is None:
        return pick(ranked), False
    path, job = history
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS picks(job TEXT UNIQUE, footage TEXT NOT NULL)")
        prior = db.execute("SELECT footage FROM picks WHERE job=?", (job,)).fetchone()
        if prior:
            same = next((clip for clip in ranked if clip["file"] == prior[0]), None)
            if same is not None:
                return same, False
        recent = {
            row[0] for row in db.execute("SELECT footage FROM picks ORDER BY rowid DESC LIMIT 3")
        }
        fresh = [
            clip
            for clip in ranked
            if clip["file"] not in recent and (clip["fit"] or not ranked[0]["fit"])
        ]
        selected = pick(fresh or ranked)
        db.execute("INSERT OR REPLACE INTO picks VALUES (?, ?)", (job, selected["file"]))
        return selected, bool(fresh and len(fresh) < len(ranked))


def decide(
    p: dict[str, Any],
    value: dict[str, Any],
    clips: list[dict[str, Any]],
    *,
    history: tuple[Path, str] | None = None,
) -> dict[str, Any]:
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

    ranked = sorted(({**c, "fit": fits(c)} for c in pool), key=lambda c: -c["fit"])
    chosen, rotated = choose_background(ranked, text, history if style != "solid" else None)
    best = chosen["fit"]
    if rotated:
        reasons.append("اختار خلفية مناسبة لم تُستخدم في آخر ثلاثة مقاطع لهذا البوت")
    name = NAMES.get(chosen["file"], chosen["title"])
    if best:
        reasons.append(f"الخلفية تناسب الموضوع: {name}")
    else:
        group = POOLS.get(footage.pool(kind), footage.pool(kind))
        reasons.append(f"لا كلمة تحدد الخلفية، فاختار من الخلفيات {group}: {name}")
    path = Path(chosen["path"]) if "path" in chosen else footage.ROOT / chosen["file"]
    brightness = luma(path)
    spots = bright_spots(path)
    by_mean = min(0.55, max(0.15, 1 - TARGET_LUMA / max(brightness, 1.0)))
    by_spots = 1 - TARGET_SPOTS / max(spots, 1.0)
    dim = round(min(MAX_DIM, max(by_mean, by_spots)), 2)
    reasons.append(
        f"سطوع الخلفية {brightness:.0f} من 255، فصار التعتيم {int(dim * 100)}٪".translate(DIGITS)
    )
    if by_spots > by_mean:
        reasons.append(
            f"بقع فاتحة خلف النص ({spots:.0f} من 255)، فزاد التعتيم ليبقى النص واضحًا".translate(
                DIGITS
            )
        )
    type_, type_reasons = typography(p)
    reasons += type_reasons
    return {
        "schema": 1,
        "agent": "designer",
        "style": style,
        "footage": chosen["file"],
        "footage_title": name,
        "footage_path": str(path),
        "footage_luma": round(brightness, 1),
        "dim": dim,
        "footage_spots": spots,
        "type": type_,
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
    found: list[dict[str, Any]] = []
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
