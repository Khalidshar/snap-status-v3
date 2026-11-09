"""Reviewer and fixer agents on the rendered clip (owner decision 8 Nov 2026: «موافق»).

The reviewer inspects the finished video and the layout report; the fixer repairs what it
can by changing the designer's decision only, then the existing second attempt renders it
again. Neither touches a letter of the text, the sound, or any religious material. Local
and free: ffmpeg and the renderer's own report, no AI and no network.
"""

import copy
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .model import SACRED

DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
SILENCE_SHARE = 0.4  # a religious clip silent for more than 40% is reported
NORMAL_SIZE = 64  # the fixer's smaller size when two pages still cut the text


def black_seconds(video: Path, duration: float, run: Callable[..., Any]) -> float:
    """Seconds of black picture between the first and last second (the title fades in)."""
    result = run(
        [
            "/usr/bin/ffmpeg",
            "-v",
            "info",
            "-nostdin",
            "-ss",
            "1",
            "-t",
            str(max(0.0, duration - 2)),
            "-i",
            str(video),
            "-vf",
            "blackdetect=d=0.5:pix_th=0.06",
            "-an",
            "-f",
            "null",
            "-",
        ],
        300,
    )
    return sum(float(x) for x in re.findall(r"black_duration:([0-9.]+)", result.stderr))


def silent_seconds(video: Path, run: Callable[..., Any]) -> float:
    result = run(
        [
            "/usr/bin/ffmpeg",
            "-v",
            "info",
            "-nostdin",
            "-i",
            str(video),
            "-af",
            "silencedetect=n=-45dB:d=1.5",
            "-vn",
            "-f",
            "null",
            "-",
        ],
        300,
    )
    return sum(float(x) for x in re.findall(r"silence_duration: ([0-9.]+)", result.stderr))


def review(
    p: dict[str, Any], layout: dict[str, Any], video: Path, run: Callable[..., Any]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    cut = [i for i in layout.get("cut", []) if isinstance(i, int)]
    if cut:
        findings.append(
            {
                "code": "CUT_TEXT",
                "cards": cut,
                "fixable": True,
                "text": f"نص مقصوص بـ«…» في {len(cut)} بطاقة".translate(DIGITS),
            }
        )
    black = black_seconds(video, p["duration"], run)
    if black >= 0.5:
        findings.append(
            {
                "code": "BLACK_FRAMES",
                "seconds": round(black, 1),
                "fixable": True,
                "text": f"إطارات سوداء {black:.1f}ث وسط المقطع".translate(DIGITS),
            }
        )
    if p["key"] in SACRED and p.get("audio"):
        silent = silent_seconds(video, run)
        if silent > SILENCE_SHARE * p["duration"]:
            findings.append(
                {
                    "code": "LONG_SILENCE",
                    "seconds": round(silent, 1),
                    "fixable": False,
                    "text": (
                        f"صمت {silent:.0f}ث من {p['duration']:.0f}ث (للعلم، لا يُضاف صوت مولّد)"
                    ).translate(DIGITS),
                }
            )
    bodies = [s.get("body") for s in p["scenes"]]
    repeated = [i for i in range(1, len(bodies)) if bodies[i] and bodies[i] == bodies[i - 1]]
    if repeated:
        findings.append(
            {
                "code": "REPEATED_CARD",
                "cards": repeated,
                "fixable": False,
                "text": f"بطاقتان متتاليتان بنفس النص ({len(repeated)})".translate(DIGITS),
            }
        )
    return findings


def fix(design: dict[str, Any], findings: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """A new designer decision for the second attempt; the text and sound stay as they are."""
    fixed = copy.deepcopy(design)
    actions: list[str] = []
    cards = fixed.setdefault("type", {"font": "body", "cards": []}).setdefault("cards", [])
    for finding in findings:
        if finding["code"] == "CUT_TEXT":
            for index in finding["cards"]:
                while len(cards) <= index:
                    cards.append({})
                card = cards[index]
                if card.get("pages") != 2:
                    card["pages"] = 2
                    actions.append(f"البطاقة {index + 1}: صفحتان بدل القص".translate(DIGITS))
                elif card.get("size", 72) > NORMAL_SIZE:
                    card["size"] = NORMAL_SIZE
                    actions.append(f"البطاقة {index + 1}: حجم أصغر ليتسع النص".translate(DIGITS))
        elif finding["code"] == "BLACK_FRAMES":
            actions.append("إعادة الرسم لإزالة الإطارات السوداء")
    fixed["fixes"] = [*design.get("fixes", []), *actions]
    return fixed, actions
