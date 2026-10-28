"""Offline admission for the unmodified King Fahd Complex Hafs font."""

import hashlib
import json
from pathlib import Path
from typing import Any

DIRECTORY = Path(__file__).resolve().parents[3] / "assets/fonts/kfgqpc"
FILENAME = "kfgqpc_hafs_v30.ttf"
FONT_SHA256 = "9ccd82324319bfe9e57efd34c830770ce86655f01661ca06b50e3b0c20073c57"
SOURCE_ZIP = "https://download.qurancomplex.gov.sa/resources_dev/kfgqpc_hafs_v30.zip"
ZIP_SHA256 = "227E6B1564D980F2BD09C2C35EBFB0330AC268C79A7C247CD1AB665BC635F245"


def verify() -> dict[str, Any]:
    """Reject missing or changed bytes, and unapproved provenance. No network."""
    try:
        manifest = json.loads((DIRECTORY / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("Missing or unreadable Quran font manifest") from error
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "schema",
            "family",
            "file",
            "sha256",
            "source_zip",
            "source_zip_sha256",
            "publication_date",
            "license_url",
            "license_note",
        }
        or type(manifest["schema"]) is not int
        or manifest["schema"] != 1
        or manifest["family"] != "kfgqpc_hafs_v3"
        or manifest["file"] != FILENAME
        or manifest["sha256"] != FONT_SHA256
        or manifest["source_zip"] != SOURCE_ZIP
        or manifest["source_zip_sha256"] != ZIP_SHA256
        or manifest["publication_date"] != "2026-09-30"
        or manifest["license_url"] != SOURCE_ZIP
        or not isinstance(manifest["license_note"], str)
        or not manifest["license_note"].strip()
    ):
        raise ValueError("Unapproved Quran font manifest")
    font = DIRECTORY / FILENAME
    try:
        if not font.is_file() or font.is_symlink():
            raise ValueError("Missing or unsafe Quran font file")
        if hashlib.sha256(font.read_bytes()).hexdigest() != manifest["sha256"]:
            raise ValueError("Quran font SHA-256 mismatch")
    except OSError as error:
        raise ValueError("Unreadable Quran font file") from error
    return manifest


def path() -> Path:
    """Return the verified bundled asset, without installing it or rendering."""
    verify()
    return DIRECTORY / FILENAME


NASKH = (
    Path(__file__).resolve().parents[3] / "assets/fonts/notonaskharabic/NotoNaskhArabic-Regular.ttf"
)
# Existing Noto Naskh Arabic Regular fingerprint from assets/fonts/manifest.json; no new font.
NASKH_SHA256 = "0919edeba540a6b27875d4651f2bf26dcbae00324b4bb034f7a030f7f294d381"


def font_for(kind_or_item: str | dict[str, Any]) -> Path:
    """Choose by content type, never by missing glyphs or by rewriting text.

    Morning/evening collections mix Quran and narration: pass the individual
    item for those collections. Unknown or contradictory types are refused.
    """
    kind: object
    if isinstance(kind_or_item, str):
        kind = kind_or_item
    elif isinstance(kind_or_item, dict):
        item = kind_or_item
        reference = "surah" in item and ("ayah" in item or "ayahs" in item)
        kind = item.get("type")
        if kind == "hadith" and reference:
            raise ValueError("Conflicting faith font type")
        if kind is None:
            if reference:
                kind = "quran"
            elif all(key in item for key in ("text", "source", "grade")):
                kind = "hadith"
    else:
        raise ValueError("Unknown faith font type")
    if kind in ("quran", "verse", "friday", "kahf"):
        return path()
    if kind not in ("hadith", "dua"):
        raise ValueError("Unknown or mixed faith kind; pass an individual item")
    try:
        if not NASKH.is_file() or NASKH.is_symlink():
            raise ValueError("Missing Noto Naskh Arabic Regular font")
        if hashlib.sha256(NASKH.read_bytes()).hexdigest() != NASKH_SHA256:
            raise ValueError("Noto Naskh Arabic Regular SHA-256 mismatch")
    except OSError as error:
        raise ValueError("Unreadable Noto Naskh Arabic Regular font") from error
    return NASKH
