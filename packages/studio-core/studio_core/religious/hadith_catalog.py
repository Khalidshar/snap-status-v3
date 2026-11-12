"""Approved data gate independent of daily selection and audio generation."""

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .hadith_text_check import verify

CATALOG = Path(__file__).with_name("data") / "hadith-calendar.json"


def load(path: Path | None = None) -> list[dict[str, Any]]:
    path = CATALOG if path is None else path
    if any(p.is_symlink() for p in (path, *path.absolute().parents)):
        raise ValueError("HADITH_CATALOG_UNSAFE")
    try:
        with path.open("rb") as stream:
            raw = stream.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("HADITH_CATALOG_INVALID")
        data = json.loads(raw)
    except (OSError, ValueError) as error:
        raise ValueError("HADITH_CATALOG_UNAVAILABLE") from error
    if not isinstance(data, dict) or data.get("approved") is not True:
        raise ValueError("HADITH_DAILY_NOT_APPROVED")
    if (
        type(data.get("schema")) is not int
        or data["schema"] != 1
        or data.get("calendar") != "umm_al_qura"
        or not isinstance(data.get("entries"), list)
        or not 1 <= len(data["entries"]) <= 366
    ):
        raise ValueError("HADITH_CATALOG_INVALID")
    seen_ids, seen_texts = set(), set()
    rows = []
    for item in data["entries"]:
        if not isinstance(item, dict):
            raise ValueError("HADITH_CATALOG_ENTRY_INVALID")
        identifier, text = item.get("id"), item.get("text")
        link = item.get("narrator_url")
        if not isinstance(link, str):
            raise ValueError("HADITH_CATALOG_ENTRY_INVALID")
        source = urlsplit(link)
        if (
            not isinstance(identifier, str)
            or not re.fullmatch("[a-z0-9_-]{1,100}", identifier)
            or not isinstance(text, str)
            or not text.strip()
            or not isinstance(item.get("sha256"), str)
            or not re.fullmatch("[0-9a-f]{64}", item["sha256"])
            or not isinstance(item.get("source_sha256"), str)
            or not re.fullmatch("[0-9a-f]{64}", item["source_sha256"])
            or item.get("approved") is not True
            or item.get("grade") != "صحيح"
            or not isinstance(item.get("source"), str)
            or not item["source"].strip()
            or not isinstance(item.get("narrator"), str)
            or not item["narrator"].strip()
            or source.scheme != "https"
            or source.netloc not in {"sunnah.com", "dorar.net"}
            or not source.path.strip("/")
            or source.query
            or source.fragment
        ):
            raise ValueError("HADITH_CATALOG_ENTRY_INVALID")
        if identifier in seen_ids or item.get("sha256") in seen_texts:
            raise ValueError("HADITH_CATALOG_DUPLICATE")
        # verify checks both hashes against the exact UTF-8 snapshot before marks.
        verify(item)
        seen_ids.add(identifier)
        seen_texts.add(item["sha256"])
        rows.append(dict(item))
    return rows
