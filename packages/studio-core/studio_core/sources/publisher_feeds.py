"""Adapt the official Arriyadiyah feed using links published on its own homepage."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from email.utils import format_datetime
from html.parser import HTMLParser
from typing import TYPE_CHECKING, cast
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from .feeds import MAX_BYTES

if TYPE_CHECKING:
    from .audit import Client

HOME = "https://arriyadiyah.com/"


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        url = urljoin(HOME, href)
        parts = urlsplit(url)
        match = re.fullmatch(r"/(\d+)/[^/?#]+", parts.path)
        if (
            match
            and parts.scheme == "https"
            and parts.netloc == "arriyadiyah.com"
            and not parts.query
            and not parts.fragment
        ):
            self.links.setdefault(match[1], url)


def arriyadiyah(raw: bytes, homepage: str) -> bytes:
    """Do not invent a slug or follow foreign links from the feed/homepage."""
    if len(raw) > MAX_BYTES or len(homepage.encode()) > MAX_BYTES:
        raise ValueError("Publisher response too large")
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", raw, re.I):
        raise ValueError("Feed DTD/entities forbidden")
    try:
        root = ET.fromstring(raw)  # noqa: S314 — DTD rejected and bytes bounded above
    except ET.ParseError as error:
        raise ValueError("Malformed publisher XML") from error
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise ValueError("Unexpected publisher feed")
    links = _Links()
    links.feed(homepage)
    for entry in list(channel.findall("item")):
        item_id = (entry.findtext("id") or "").strip()
        original = (entry.findtext("link") or "").strip()
        if (
            not item_id.isascii()
            or not item_id.isdigit()
            or item_id not in links.links
            or original not in {HOME + item_id, links.links[item_id]}
        ):
            channel.remove(entry)
            continue
        try:
            stamp = datetime.strptime(entry.findtext("publication_time") or "", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            channel.remove(entry)
            continue
        link = entry.find("link")
        if link is None:
            raise ValueError("Publisher item link missing")
        link.text = links.links[item_id]
        date = entry.find("pubDate")
        if date is None:
            date = ET.SubElement(entry, "pubDate")
        date.text = format_datetime(stamp.replace(tzinfo=ZoneInfo("Asia/Riyadh")))
        image = (entry.findtext("main_image") or "").strip()
        parts = urlsplit(image)
        if parts.scheme == "https" and parts.netloc == "arriyadiyah.com":
            ET.SubElement(entry, "enclosure", {"url": image})
    return cast(bytes, ET.tostring(root, encoding="utf-8"))


def normalize(source_id: str, raw: bytes, client: Client) -> bytes:
    if source_id != "arriyadiyah":
        return raw
    homepage = client.get(HOME)
    if (
        not 200 <= homepage.status < 300
        or hashlib.sha256(homepage.body).hexdigest() != homepage.sha256
    ):
        raise ValueError("Invalid publisher homepage")
    try:
        return arriyadiyah(raw, homepage.body.decode("utf-8"))
    except UnicodeError as error:
        raise ValueError("Invalid publisher encoding") from error
