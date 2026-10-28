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
