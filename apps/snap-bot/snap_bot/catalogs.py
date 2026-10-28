"""Reviewed variety catalogs. Only bundled JSON is read; no network or selection."""

import json
import re
import unicodedata
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .content_agents import proofread, translation_requirement
from .did_you_know import safe_text

KINDS = ("tech", "style", "riddle", "funny", "night")
DATA = Path(__file__).with_name("data")
HOSTS = {
    "www.aad.org",
    "extension.purdue.edu",
    "career.charlotte.edu",
    "www.brenau.edu",
    "www.cisa.gov",
    "www.ncsc.gov.uk",
    "support.apple.com",
    "support.google.com",
    "support.microsoft.com",
    "developer.mozilla.org",
    "support.mozilla.org",
    "www.ginetex.net",
    "www.productsafety.gov.au",
    "www.ised-isde.canada.ca",
}


def body(item: dict[str, Any], kind: str) -> str:
    """Return the exact rendered text, including the riddle's answer line."""
    if kind not in KINDS:
        raise ValueError("Unknown catalog kind")
    return f"{item['question']}\n{item['answer']}" if kind == "riddle" else str(item["text"])


def _text(value: object, limit: int) -> None:
    safe_text(value, limit)
    if not isinstance(value, str):
        raise ValueError("Invalid catalog text")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise ValueError("Unsafe catalog text")


def load(kind: str) -> list[dict[str, Any]]:
    """Validate the entire catalog before returning any item."""
    if kind not in KINDS:
        raise ValueError("Unknown catalog kind")
    try:
        data = json.loads((DATA / f"{kind}.ar.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Unreadable catalog") from error
    if (
        not isinstance(data, dict)
        or set(data) != {"schema", "language", "sacred", "kind", "items"}
        or type(data["schema"]) is not int
        or data["schema"] != 1
        or data["language"] != "ar"
        or data["sacred"] is not False
        or data["kind"] != kind
    ):
        raise ValueError("Unapproved catalog")
    items = data["items"]
    if not isinstance(items, list) or len(items) != 30:
        raise ValueError("Catalog requires 30 items")
    ids: set[str] = set()
    texts: set[str] = set()
    today = datetime.now(UTC).date()
    for item in items:
        keys = {"id", "source", "verified_at"}
        keys |= {"question", "answer"} if kind == "riddle" else {"text"}
        if kind in {"tech", "style"}:
            keys.add("source_url")
        if not isinstance(item, dict) or set(item) != keys:
            raise ValueError("Incomplete catalog item")
        identifier = item["id"]
        if not isinstance(identifier, str) or not re.fullmatch(rf"{kind}-[0-9]{{2}}", identifier):
            raise ValueError("Invalid catalog ID")
        if kind == "riddle":
            _text(item["question"], 120)
            _text(item["answer"], 60)
            unique_text = item["question"]
        else:
            _text(item["text"], 120)
            unique_text = item["text"]
        if identifier in ids or unique_text in texts:
            raise ValueError("Duplicate catalog item")
        ids.add(identifier)
        texts.add(unique_text)
        _text(item["source"], 80)
        if kind in {"tech", "style"}:
            _text(item["source_url"], 512)
            url = urlsplit(item["source_url"])
            if (
                url.scheme != "https"
                or url.hostname not in HOSTS
                or url.username is not None
                or url.password is not None
                or url.port not in (None, 443)
                or url.path in {"", "/"}
            ):
                raise ValueError("Unapproved catalog source")
        elif item["source"] != "original":
            raise ValueError("Original source required")
        verified = item["verified_at"]
        if not isinstance(verified, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", verified
        ):
            raise ValueError("Invalid verification date")
        if date.fromisoformat(verified) > today:
            raise ValueError("Future verification date")
        rendered = body(item, kind)
        proofread(rendered)
        translation_requirement(rendered)
    return items
