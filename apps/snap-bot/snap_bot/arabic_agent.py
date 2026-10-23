"""Deterministic Arabic fact checks. No rewriting, network or AI service."""

import itertools
import re
import textwrap
import unicodedata
from typing import Any

RULE_VERSION = "arabic-local-v1"
ABBREVIATIONS = ("E Ink", "IPv6", "IPv4", "CERN", "GPS", "NFC", "QR")
CHECKS = (
    "arabic_letters",
    "known_latin",
    "arabic_punctuation",
    "line_limits",
    "no_adjacent_duplicates",
)


def check_text(text: object) -> dict[str, Any]:
    if not isinstance(text, str) or not text or len(text) > 120 or text != text.strip():
        raise ValueError("ARABIC_TEXT_INVALID")
    # Inspect NFC, without modifying catalog/content bytes or their hashes.
    normalized = unicodedata.normalize("NFC", text)
    remaining = re.sub(
        r"(?<!\w)(?:" + "|".join(re.escape(x) for x in ABBREVIATIONS) + r")(?!\w)", "", normalized
    )
    if re.search(r"[A-Za-z]", remaining):
        raise ValueError("ARABIC_LATIN_UNAPPROVED")
    # The one exception for ASCII commas is an intact, grouped number.
    punctuation = re.sub(r"(?<![\w,])\d{1,3}(?:,\d{3})+(?![\w,])", "0", remaining)
    if any(c in punctuation for c in ",;?"):
        raise ValueError("ARABIC_PUNCTUATION_INVALID")
    if not any(
        "\u0621" <= c <= "\u064a" and unicodedata.category(c).startswith("L") for c in normalized
    ):
        raise ValueError("ARABIC_LETTERS_MISSING")
    previous = ""
    for char in punctuation:
        arabic_letter = "\u0621" <= char <= "\u063a" or "\u0641" <= char <= "\u064a"
        mark = 0x064B <= ord(char) <= 0x0655 or ord(char) == 0x0670
        if mark and not (
            previous
            and (
                unicodedata.category(previous).startswith("L")
                or unicodedata.category(previous).startswith("M")
            )
        ):
            raise ValueError("ARABIC_MARK_ORPHAN")
        if not (arabic_letter or mark or char in " 0123456789٠١٢٣٤٥٦٧٨٩،؛؟.:!()«»،٫٬-"):
            raise ValueError("ARABIC_CHARACTER_INVALID")
        previous = char
    lines = textwrap.wrap(text, width=26, break_long_words=False, break_on_hyphens=False)
    if not 1 <= len(lines) <= 5 or any(len(line) > 26 for line in lines):
        raise ValueError("ARABIC_LINE_LIMIT")
    plain = "".join(c for c in normalized if not (0x064B <= ord(c) <= 0x0655 or ord(c) == 0x0670))
    words = re.findall(r"[\u0621-\u063a\u0641-\u064a]+|[A-Za-z]+", plain)
    if any(a.casefold() == b.casefold() for a, b in itertools.pairwise(words)):
        raise ValueError("ARABIC_DUPLICATE_WORD")
    return {
        "version": RULE_VERSION,
        "decision": "PASS",
        "checks": list(CHECKS),
        "line_lengths": [len(line) for line in lines],
    }


def inspect_content(content: object) -> dict[str, Any]:
    if (
        not isinstance(content, dict)
        or content.get("type") != "did_you_know"
        or content.get("sacred") is not False
    ):
        raise ValueError("ARABIC_CONTENT_INVALID")
    return check_text(content.get("body"))


def check_content(content: dict[str, Any]) -> dict[str, Any]:
    inspect_content(content)
    return dict(content)
