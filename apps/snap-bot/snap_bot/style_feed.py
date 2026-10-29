"""Offline RSS/Atom style headlines. No fetching, translation, rendering or delivery."""

import hashlib
import html
import json
import re
import unicodedata
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

DATA = Path(__file__).with_name("data") / "style_sources.json"
ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/elements/1.1/}"
MAX_BYTES = 2_000_000
KEYWORDS = re.compile(
    r"\b(?:fashion|beauty|makeup|make-up|cosmetics?|perfumes?|fragrances?|"
    r"couture|apparel|clothing|collections?|skincare|lipstick|sneakers?|"
    r"sportswear|streetwear|footwear|handbags?)\b|"
    r"موضة|أزياء|ازياء|مكياج|تجميل|عطور|عطر|ملابس|أحذية|حقائب|ماركات|مجوهرات",
    re.IGNORECASE,
)
STYLE_SECTIONS = {"fashion", "beauty", "fragrance", "موضة", "أزياء", "جمال", "عطور"}


def _plain(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing feed text")
    # Inspect controls before stripping, including entity-encoded HTML/control text.
    for _ in range(3):
        if any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value):
            raise ValueError("Unsafe feed character")
        if "<" in value or ">" in value:
            raise ValueError("HTML in feed text")
        decoded = html.unescape(value)
        if decoded == value:
            return value.strip()
        value = decoded
    raise ValueError("Nested feed encoding")


def _url(value: object) -> str:
    url = _plain(value)
    _plain(unquote(url))
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or any(c.isspace() for c in url)
    ):
        raise ValueError("Unsafe feed URL")
    return url


def _source(source: dict[str, Any]) -> dict[str, Any]:
    _url(source.get("url"))
    if len(_plain(source.get("name"))) > 20 or source.get("language") not in ("ar", "en"):
        raise ValueError("Invalid style source")
    _plain(source.get("section"))
    return source


def sources() -> list[dict[str, Any]]:
    """Reviewed official feeds only; excluded candidates live in the PR survey."""
    obj = json.loads(DATA.read_text(encoding="utf-8"))
    if obj.get("schema") != 1 or not isinstance(obj.get("sources"), list):
        raise ValueError("Invalid style source catalog")
    result = [_source(source) for source in obj["sources"]]
    if len({source["url"] for source in result}) != len(result):
        raise ValueError("Duplicate style source")
    return result


def title_fingerprint(title: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFC", _plain(title)).casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def deduplicate(items: list[dict[str, str]], seen: set[str] | None = None) -> list[dict[str, str]]:
    """Share/persist seen in the caller to suppress duplicates across feeds or polls."""
    known = seen if seen is not None else set()
    result = []
    for item in items:
        fingerprint = title_fingerprint(item["title"])
        if fingerprint not in known:
            result.append(item)
            known.add(fingerprint)
    return result


def _field(entry: ET.Element, names: tuple[str, ...]) -> str:
    for name in names:
        node = entry.find(name)
        if node is not None:
            if list(node):
                raise ValueError("Markup in feed field")
            return _plain(node.text)
    raise ValueError("Missing feed field")


def _date(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        raise ValueError("Feed date needs timezone")
    return parsed.astimezone(UTC).isoformat()


def parse_feed(xml_bytes: bytes, source: dict[str, Any]) -> list[dict[str, str]]:
    """Return newest plain headlines first; discard invalid/off-topic entries.

    RSS 2 and Atom 1 only. DTDs/entities and oversized or malformed documents fail
    closed. Embedded summaries/images are never used. English stays untranslated.
    Source is caller-supplied metadata from the reviewed catalog, not feed content.
    """
    _source(source)
    if not isinstance(xml_bytes, bytes) or len(xml_bytes) > MAX_BYTES:
        raise ValueError("Invalid feed size/type")
    try:
        document = xml_bytes.decode("utf-8-sig")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", document, re.IGNORECASE):
            raise ValueError("Feed DTD/entities forbidden")
        root = ET.fromstring(document)  # noqa: S314  # DTD forbidden, size bounded above
    except (UnicodeError, ET.ParseError) as error:
        raise ValueError("Malformed feed XML") from error
    if root.tag == "rss":
        entries = root.findall("./channel/item")
        atom = False
    elif root.tag == ATOM + "feed":
        entries = root.findall(ATOM + "entry")
        atom = True
    else:
        raise ValueError("Unsupported feed format")
    if len(entries) > 1000:
        raise ValueError("Too many feed entries")
    result = []
    for entry in entries:
        try:
            title = _field(entry, (ATOM + "title",) if atom else ("title",))
            if atom:
                title_node = entry.find(ATOM + "title")
                if title_node is not None and title_node.get("type", "text") != "text":
                    raise ValueError("HTML Atom title")
                link = next(
                    node.get("href")
                    for node in entry.findall(ATOM + "link")
                    if node.get("rel", "alternate") == "alternate"
                )
                published = _field(entry, (ATOM + "published", ATOM + "updated"))
                categories = [_plain(n.get("term")) for n in entry.findall(ATOM + "category")]
            else:
                link = _field(entry, ("link",))
                published = _field(entry, ("pubDate", DC + "date"))
                categories = [_plain(n.text) for n in entry.findall("category")]
            if source["section"].casefold() not in STYLE_SECTIONS and not KEYWORDS.search(
                " ".join([title, *categories])
            ):
                continue
            result.append(
                {
                    "title": title,
                    "link": _url(link),
                    "date": _date(published),
                    "source": source["name"],
                }
            )
        except (ValueError, TypeError, StopIteration, OverflowError):
            continue
    result.sort(key=lambda item: item["date"], reverse=True)
    return deduplicate(result)
