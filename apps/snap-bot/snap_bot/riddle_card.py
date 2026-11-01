"""Timed reviewed riddle: question, five one-second digits, then the answer."""

import textwrap
from typing import Any

from .did_you_know import validate_content

Label = tuple[str, int, int, str, str]


def layout(
    content: dict[str, Any], duration: float
) -> tuple[list[Label], list[tuple[float, float]]]:
    validate_content(content)
    if content.get("kind") != "riddle" or not 8 <= duration <= 12:
        raise ValueError("Invalid riddle profile")
    question, answer = content["body"].split("\n")
    labels: list[Label] = [
        ("روّق مع خالد", 50, 210, "0xD6B26C", "arabic"),
        ("@rawwag_khalid", 38, 290, "0xD6B26C", "latin"),
        ("لغز اليوم", 96, 430, "0xD6B26C", "arabic"),
    ]
    windows = [(0.0, duration)] * len(labels)
    for text, start, end, top in ((question, 0.0, 6.0, 650), (answer, 6.0, duration, 850)):
        lines = textwrap.wrap(text, width=26, break_long_words=False, break_on_hyphens=False)
        if not 1 <= len(lines) <= (5 if start == 0 else 3) or any(len(line) > 26 for line in lines):
            raise ValueError("Riddle exceeds safe line limits")
        for index, line in enumerate(lines):
            labels.append((line, 52, top + index * 100, "0xF6EEDE", "arabic"))
            windows.append((start, end))
    labels.append(("الإجابة", 76, 650, "0xD6B26C", "arabic"))
    windows.append((6.0, duration))
    for second, digit in enumerate("٥٤٣٢١", 1):
        labels.append((digit, 96, 1180, "0xD6B26C", "arabic"))
        windows.append((float(second), float(second + 1)))
    # Riddles are original: the brand at the top already names the source.
    labels.append(("تابعني للمزيد", 56, 1480, "0xF6EEDE", "arabic"))
    windows.append((0.0, duration))
    return labels, windows
