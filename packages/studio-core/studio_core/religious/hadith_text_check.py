"""Read-only mark coverage and byte-exact source checks; never vocalize sacred text."""

import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Any

SOURCES = Path(__file__).with_name("data") / "hadith-sources"
VOWELS = frozenset("ًٌٍَُِْ")


def _letters(text: str) -> list[tuple[int, str, str, int]]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("HADITH_TEXT_EMPTY")
    letters: list[tuple[int, str, str, int]] = []
    word, attached = 0, False
    for index, char in enumerate(text):
        category = unicodedata.category(char)
        if category in {"Cc", "Cf", "Cs"}:
            raise ValueError("HADITH_TEXT_UNSAFE")
        if char == "ـ":
            continue
        if "ء" <= char <= "ي" or char == "ٱ":
            letters.append((index, char, "", word))
            attached = True
        elif category.startswith("M"):
            if not attached:
                raise ValueError("HADITH_DIACRITIC_ORPHANED")
            at, letter, marks, group = letters[-1]
            letters[-1] = (at, letter, marks + char, group)
        else:
            if category.startswith("L"):
                raise ValueError("HADITH_TEXT_UNSUPPORTED_LETTER")
            word += 1
            attached = False
    if not letters:
        raise ValueError("HADITH_TEXT_EMPTY")
    return letters


def missing_positions(text: str) -> list[int]:
    """Report unmarked letters for review; this list is never auto-approved."""
    return [index for index, _, marks, _ in _letters(text) if not set(marks) & VOWELS]


def verify_diacritics(text: str, exceptions: Any) -> None:
    letters = _letters(text)
    if (
        not isinstance(exceptions, list)
        or any(type(index) is not int for index in exceptions)
        or len(set(exceptions)) != len(exceptions)
    ):
        raise ValueError("HADITH_DIACRITIC_EXCEPTIONS_INVALID")
    missing: set[int] = set()
    permitted: set[int] = set()
    for n, (index, char, marks, word) in enumerate(letters):
        if any(mark not in VOWELS | {"ّ", "ٰ"} for mark in marks):
            raise ValueError("HADITH_DIACRITIC_UNSUPPORTED")
        vowels = [mark for mark in marks if mark in VOWELS]
        if len(vowels) > 1 or marks.count("ّ") > 1:
            raise ValueError("HADITH_DIACRITICS_CONFLICT")
        if vowels:
            continue
        missing.add(index)
        if "ّ" in marks:
            continue  # A shadda never replaces the vowel of a consonant.
        before = letters[n - 1] if n and letters[n - 1][3] == word else None
        after = letters[n + 1] if n + 1 < len(letters) and letters[n + 1][3] == word else None
        previous_marks = set(before[2]) if before else set()
        # Only reviewed long-vowel/wasl/article conventions may be exempted.
        # The source bytes and the approved exception offsets are both pinned.
        allowed = (
            (char in {"ا", "ى"} and bool(previous_marks & set("ًَ")))
            or (char == "و" and bool(previous_marks & set("ٌُ")))
            or (char == "ي" and bool(previous_marks & set("ٍِ")))
            or (char in {"ٱ", "آ"})
            or (
                char == "ا"
                and before is None
                and after is not None
                and (after[1] == "ل" or "ْ" in after[2])
            )
            or (char == "ل" and before is not None and before[1] in {"ا", "ٱ"})
            or (char == "ل" and after is not None and after[1] == "ا" and "َ" in after[2])
        )
        if allowed:
            permitted.add(index)
    exempt = set(exceptions)
    if not exempt <= permitted:
        raise ValueError("HADITH_DIACRITIC_EXCEPTIONS_INVALID")
    if missing - exempt:
        raise ValueError("HADITH_DIACRITICS_MISSING")


def verify(item: dict[str, Any], *, root: Path | None = None) -> None:
    identifier, text = item["id"], item["text"]
    if not isinstance(identifier, str) or not re.fullmatch("[a-z0-9_-]{1,100}", identifier):
        raise ValueError("HADITH_SOURCE_ID_INVALID")
    if not isinstance(text, str):
        raise ValueError("HADITH_TEXT_EMPTY")
    root = SOURCES if root is None else root
    path = root / (identifier + ".txt")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("HADITH_SOURCE_UNSAFE")
    try:
        with path.open("rb") as stream:
            original = stream.read(64_001)
    except OSError as error:
        raise ValueError("HADITH_SOURCE_SNAPSHOT_MISSING") from error
    if (
        len(original) > 64_000
        or hashlib.sha256(original).hexdigest() != item["source_sha256"]
        or original != text.encode("utf-8")
        or hashlib.sha256(text.encode()).hexdigest() != item["sha256"]
    ):
        raise ValueError("HADITH_SOURCE_TEXT_CHANGED")
    verify_diacritics(text, item.get("unmarked_positions"))
