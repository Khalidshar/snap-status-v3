"""Verbatim faith data, checked before use. No network, rewriting or proofreading.

The bundled subset comes from the owner-approved 2026-09-30 package.
Missing or invalid data fails closed; there is no fallback text or online fetch.
"""

import hashlib
import json
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

DATA = Path(__file__).with_name("data") / "faith"
SOURCE_REPO = "Khalidshar/status-bot@b445281"
OFFICIAL_URL = "https://download.qurancomplex.gov.sa/resources_dev/kfgqpc_hafs_v30.zip"
OFFICIAL_SHA256 = "227E6B1564D980F2BD09C2C35EBFB0330AC268C79A7C247CD1AB665BC635F245"
PREVIOUS_SHA256 = "6D4B6B2B6A290BDA1D8E3179BC6AAB7D49F6FAEDD3F2D31F7D589D5E6E99F8F4"
KINDS = ("adhkar_am", "adhkar_pm", "hadith", "dua", "verse", "friday", "kahf")
GRADES = ("صحيح", "حسن صحيح", "حسن")


def _safe(value: object, *, quran: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Missing faith string")
    # U+06DD is the official ayah-number marker, not a bidi control. Keep it
    # verbatim only for Quran text, once, immediately before final Arabic digits.
    ayah_marker = quran and value.count("\u06dd") == 1 and re.search(r"\u06dd[٠-٩]+\Z", value)
    if any(
        unicodedata.category(c) in {"Cc", "Cf", "Cs"} and not (c == "\u06dd" and ayah_marker)
        for c in value
    ):
        raise ValueError("Unsafe faith character")
    return value


def _verified(item: dict[str, Any], *, quran: bool = False) -> str:
    value = _safe(item.get("text"), quran=quran)
    if item.get("sha256") != hashlib.sha256(value.encode("utf-8")).hexdigest():
        raise ValueError("Faith text hash mismatch")
    return value


def _document(filename: str, kind: str) -> dict[str, Any]:
    try:
        obj = json.loads((DATA / filename).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("Missing or unreadable faith data: " + filename) from error
    if (
        not isinstance(obj, dict)
        or type(obj.get("schema")) is not int
        or obj["schema"] != 1
        or obj.get("language") != "ar"
        or obj.get("sacred") is not True
        or obj.get("kind") != kind
        or obj.get("source_repo") != SOURCE_REPO
        or not isinstance(obj.get("items"), list)
        or not obj["items"]
    ):
        raise ValueError("Unapproved faith catalog")
    return obj


def _ids(items: list[Any]) -> None:
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid faith item")
        identifier = _safe(item.get("id"))
        if not re.fullmatch(r"[a-z0-9_-]+", identifier) or identifier in seen:
            raise ValueError("Invalid or duplicate faith ID")
        seen.add(identifier)
        # Inspect all string metadata too, without modifying it.
        for value in item.values():
            if isinstance(value, str):
                _safe(value)


def _references(item: dict[str, Any], index: dict[tuple[int, int], str]) -> str:
    surah = item.get("surah")
    ayahs = item.get("ayahs")
    if (
        type(surah) is not int
        or not 1 <= surah <= 114
        or not isinstance(ayahs, list)
        or not ayahs
        or any(type(a) is not int or a < 1 for a in ayahs)
        or len(set(ayahs)) != len(ayahs)
        or "text" in item
        or "sha256" in item
    ):
        raise ValueError("Invalid Quran references")
    try:
        return " ".join(index[(surah, a)] for a in ayahs)
    except KeyError as error:
        raise ValueError("Missing referenced ayah") from error


def _subset() -> tuple[dict[tuple[int, int], str], dict[str, Any]]:
    obj = _document("quran_subset.json", "quran_subset")
    if (
        obj.get("zip_url") != OFFICIAL_URL
        or obj.get("zip_sha256") != OFFICIAL_SHA256
        or obj.get("publication_date") != "2026-09-30"
        or obj.get("previous_zip_sha256") != PREVIOUS_SHA256
        or not isinstance(obj.get("checksum_note"), str)
        or not obj["checksum_note"]
        or obj.get("package_version") != "kfgqpc_hafs_v30"
        or obj.get("package_ayahs") != 6236
        or obj.get("package_surahs") != 114
    ):
        raise ValueError("Unapproved official Quran package")
    try:
        date.fromisoformat(obj["build_date"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Invalid subset build date") from error
    index: dict[tuple[int, int], str] = {}
    for item in obj["items"]:
        if not isinstance(item, dict) or set(item) != {"surah", "ayah", "text", "sha256"}:
            raise ValueError("Invalid subset ayah")
        surah, ayah = item["surah"], item["ayah"]
        if type(surah) is not int or not 1 <= surah <= 114 or type(ayah) is not int or ayah < 1:
            raise ValueError("Invalid ayah coordinates")
        if (surah, ayah) in index:
            raise ValueError("Duplicate subset ayah")
        index[(surah, ayah)] = _verified(item, quran=True)
    if len(index) != 90:
        raise ValueError("Incomplete Quran subset")
    collections = obj.get("collections")
    if not isinstance(collections, dict) or set(collections) != {"verse", "friday", "kahf"}:
        raise ValueError("Missing Quran collections")
    for kind, count in (("verse", 46), ("friday", 3), ("kahf", 1)):
        items = collections[kind]
        if not isinstance(items, list) or len(items) != count:
            raise ValueError("Incomplete Quran collection")
        _ids(items)
        for item in items:
            _references(item, index)
    kahf = collections["kahf"][0]
    if kahf["surah"] != 18 or kahf["ayahs"] != list(range(1, 11)):
        raise ValueError("Invalid Kahf reference")
    return index, collections


def load(kind: str) -> list[dict[str, Any]]:
    """Validate the entire relevant catalog before returning any display item."""
    if kind not in KINDS:
        raise ValueError("Unknown faith kind")
    if kind in {"verse", "friday", "kahf"}:
        _, collections = _subset()
        result: list[dict[str, Any]] = collections[kind]
        return result
    catalog = "adhkar" if kind.startswith("adhkar_") else kind
    filename = "duas.json" if catalog == "dua" else catalog + ".json"
    items: list[dict[str, Any]] = _document(filename, catalog)["items"]
    _ids(items)
    if len(items) != {"adhkar": 20, "hadith": 8, "dua": 5}[catalog]:
        raise ValueError("Incomplete faith catalog")
    index = _subset()[0] if catalog == "adhkar" else {}
    for item in items:
        _safe(item.get("source"))
        if catalog == "adhkar":
            if (
                item.get("period") not in ("am", "pm")
                or item.get("type") not in ("quran", "hadith")
                or type(item.get("count")) is not int
                or item["count"] < 1
            ):
                raise ValueError("Invalid adhkar metadata")
            _safe(item.get("verification_url"))
            if item["type"] == "quran":
                _references(item, index)
            else:
                _verified(item)
        else:
            if item.get("grade") not in GRADES:
                raise ValueError("Unapproved narration grade")
            _verified(item)
    if catalog == "adhkar":
        if sum(i["period"] == "am" for i in items) != 9:
            raise ValueError("Incomplete morning adhkar")
        return [i for i in items if i["period"] == kind.removeprefix("adhkar_")]
    return items


def text(item: dict[str, Any]) -> str:
    """Exact source text; Quran ayahs are joined only by one literal space."""
    if not isinstance(item, dict):
        raise ValueError("Invalid faith item")
    if "surah" in item or "ayahs" in item or item.get("type") == "quran":
        return _references(item, _subset()[0])
    return _verified(item)
