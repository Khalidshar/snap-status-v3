"""Public-source funnel counts; never reserves history, approves images or renders."""

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

from studio_core.production.fashion_policy import TITLES, approved_brand, matches_desk
from studio_core.sources import for_command
from studio_core.sources.news import MAX_AGE, _link, news_sources

from . import parsing
from .prepare_base import safe_xml
from .prepare_news import NewsPreparation

STAGES = ("raw", "age", "language", "brand", "desk", "image", "deduplicated")


def feed_candidates(source: Any, raw: bytes, now: float) -> list[dict[str, Any]]:
    """Keep rejected ages/languages for the funnel, using production XML/date rules."""
    out = []
    xml = re.sub(rb"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", b"&amp;", raw)
    for entry in safe_xml(xml).iter("item"):
        title = parsing.clean(entry.findtext("title") or "")
        detail = parsing.clean(
            entry.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
            or entry.findtext("description")
            or ""
        )
        url = (entry.findtext("link") or "").strip()
        published: float | None
        try:
            _link(url, source)
            published = parsing.feed_time(entry.findtext("pubDate") or "")
        except ValueError:
            published = None
        out.append(
            {
                "title": title,
                "detail": detail,
                "url": url,
                "image": parsing._img(entry),
                "source_id": source.id,
                "language": source.language,
                "age_ok": published is not None and 0 <= now - published < MAX_AGE["style"],
                "language_ok": bool(title)
                and NewsPreparation.editorial_match(
                    "style", title, source.language, detail=detail, source_id=source.id
                ),
            }
        )
    return out


def funnel(
    candidates: list[dict[str, Any]],
    image_available: Callable[[dict[str, Any]], bool],
    used: Callable[[str, str], bool] = lambda key, url: False,
) -> list[dict[str, Any]]:
    rows = []
    for key, title in TITLES.items():
        pool = candidates
        counts = {"raw": len(pool)}

        def in_desk(item: dict[str, Any], desk: str = key) -> bool:
            return matches_desk(desk, item["title"], item["detail"])

        filters: tuple[tuple[str, Callable[[dict[str, Any]], bool]], ...] = (
            ("age", lambda item: item["age_ok"]),
            ("language", lambda item: item["language_ok"]),
            ("brand", lambda item: approved_brand(item["title"] + " " + item["detail"])),
            ("desk", in_desk),
            ("image", image_available),
        )
        for stage, predicate in filters:
            pool = [item for item in pool if predicate(item)]
            counts[stage] = len(pool)
        seen: list[str] = []
        unique = []
        for item in pool:
            topic = item["title"] + " " + item["detail"][:120]
            if not used(key, item["url"]) and not parsing.same_topic(topic, seen):
                unique.append(item)
                seen.append(topic)
        counts["deduplicated"] = len(unique)
        rows.append({"command": key, "title": title, **counts})
    fallback = rows[0]["deduplicated"]
    for row in rows:
        row["fallback_pool"] = fallback if not row["deduplicated"] else 0
    return rows


def diagnose(
    client: Any,
    *,
    now: float | None = None,
    website_reader: Callable[[Any], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Fetch every approved source once; each failed fetch is explicit, never a fake zero."""
    now = time.time() if now is None else now
    candidates: list[dict[str, Any]] = []
    sources = []
    production_ids = {source.id for source in news_sources("style", production=True)}
    for source in for_command("style"):
        if source.status != "approved" or source.kind not in {"rss", "website"}:
            continue
        row: dict[str, Any] = {"source": source.id, "kind": source.kind}
        try:
            response = client.get(source.url)
            row["status"] = response.status
            if response.status != 200:
                row["result"] = "UNAVAILABLE"
            elif source.id not in production_ids:
                row["result"] = "FETCHED_NOT_IN_PRODUCTION_CANDIDATE_PATH"
            elif source.kind == "rss":
                items = feed_candidates(source, response.body, now)
                candidates.extend(items)
                row.update(result="RSS", raw=len(items))
            elif website_reader is not None:
                items = website_reader(source)
                for item in items:
                    item.update(age_ok=True, language_ok=True)
                candidates.extend(items)
                row.update(result="WEBSITE_AFTER_EXISTING_ADAPTER", raw=len(items))
            else:
                row["result"] = "NOT_RSS"
        except (OSError, ValueError, RuntimeError, ET.ParseError):
            row["result"] = "FETCH_OR_PARSE_FAILED"
        sources.append(row)

    image_cache: dict[str, bool] = {}

    def image_available(item: dict[str, Any]) -> bool:
        url = item["url"]
        if url not in image_cache:
            try:
                page = client.get(url).body.decode("utf-8", "replace")
                match = re.search(
                    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                    page,
                    re.I,
                )
                image = item.get("image") or (match[1] if match else "")
                raw = client.get(image).body if image else b""
                image_cache[url] = raw.startswith((b"\xff\xd8", b"\x89PNG")) or (
                    raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
                )
            except (OSError, ValueError, RuntimeError):
                image_cache[url] = False
        return image_cache[url]

    return {
        "checked_at": now,
        "sources": sources,
        "rows": funnel(candidates, image_available),
        "history_scope": "candidate_topics_only; device history not read",
        "image_scope": "public image bytes only; owner review and render not verified",
        "website_scope": (
            "website adapter already applies age/language; raw counts are post-adapter"
        ),
        "complete": all("raw" in row for row in sources),
    }
