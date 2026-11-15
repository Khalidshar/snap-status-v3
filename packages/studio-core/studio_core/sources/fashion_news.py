"""Read-only fashion reviews for the command collector, with a per-source funnel."""

import hashlib
import html
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from studio_core.network.client import NetworkError
from studio_core.production import parsing
from studio_core.production.fashion_policy import BRANDS
from studio_core.production.prepare_base import safe_xml
from studio_core.sources.feeds import parse_feed

STAGES = ("raw", "parsed", "title", "language", "brand", "section", "age", "seen", "predicate")


def language_of(title: str, declared: str) -> str | None:
    from .news import arabic

    if arabic(title):
        return "ar"
    if declared == "ar":
        remaining = title
        for aliases in BRANDS:
            for alias in aliases:
                remaining = re.sub(re.escape(alias), "", remaining, flags=re.IGNORECASE)
        return "ar" if arabic(remaining) else None
    return "en" if declared == "en" and re.search("[A-Za-z]", title) else None


def website_items(
    source: Any, body: bytes, client: Any, row: dict[str, Any]
) -> list[dict[str, Any]]:
    """Use public same-publisher article metadata; never an invented RSS endpoint."""
    links = []
    for href in re.findall(r'href=["\']([^"\']+)', body.decode("utf-8", "replace")):
        url = urljoin(source.url, html.unescape(href)).split("#")[0]
        if (
            urlsplit(url).hostname == urlsplit(source.url).hostname
            and len(urlsplit(url).path) >= 40
            and url not in links
            and not any(x in urlsplit(url).path for x in ("/assets/", "/files/", "/videos/"))
        ):
            links.append(url)
    row["counts"]["raw"] = min(18, len(links))
    items = []
    for url in links[:18]:
        try:
            response = client.get(url)
            if (
                response.status != 200
                or hashlib.sha256(response.body).hexdigest() != response.sha256
            ):
                raise ValueError("ARTICLE_RESPONSE_INVALID")
            page = response.body.decode("utf-8", "replace")
            title = re.search(
                r'(?:property|name)=["\']og:title["\'][^>]+content=["\']([^"\']+)', page, re.I
            )
            date = re.search(r'"datePublished"\s*:\s*"([^"<>]+)"', page) or re.search(
                r'property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)', page
            )
            if title is None or date is None:
                row["article_metadata_missing"] = row.get("article_metadata_missing", 0) + 1
                continue
            items.append({"title": parsing.clean(title[1]), "link": url, "date": date[1]})
        except (NetworkError, OSError, ValueError):
            row["article_fetch_failed"] = row.get("article_fetch_failed", 0) + 1
    return items


def collect_fashion(
    key: str,
    client: Any,
    now: float,
    *,
    seen: frozenset[str],
    source_ids: frozenset[str] | None,
    predicate: Callable[[str], bool] | None,
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    from .news import (
        AUDIO_POLICY,
        MAX_AGE,
        MEDIA_POLICY,
        TITLES,
        NewsUnavailable,
        _link,
        digest,
        fingerprint,
        matches,
        news_sources,
        published_label,
        top_brand,
    )

    candidates = []
    failures = []
    for source in news_sources(key, production=True):
        row: dict[str, Any] = {
            "source_id": source.id,
            "kind": source.kind,
            "counts": dict.fromkeys(STAGES, 0),
            "fallback_accepted": 0,
        }
        diagnostics.append(row)
        if source_ids is not None and source.id not in source_ids:
            row["reason"] = "SOURCE_ID_FILTER"
            continue
        try:
            response = client.get(source.url)
            row["http_status"] = response.status
            if not 200 <= response.status < 300:
                row["reason"] = "UPSTREAM_HTTP_" + str(response.status)
                continue
            if hashlib.sha256(response.body).hexdigest() != response.sha256:
                raise ValueError("SOURCE_HASH_INVALID")
            if source.kind == "website":
                items = website_items(source, response.body, client, row)
            else:
                from .publisher_feeds import normalize

                raw = normalize(source.id, response.body, client)
                document = safe_xml(raw)
                row["counts"]["raw"] = len(list(document.iter("item")))
                items = parse_feed(
                    raw,
                    {
                        "name": source.name,
                        "url": source.url,
                        "language": source.language,
                        "section": "news",
                    },
                    style_only=False,
                )
            row["counts"]["parsed"] = len(items)
            row["reason"] = "PARSED"
            for item in items:
                title = item["title"]
                counts = row["counts"]
                if not 1 <= len(title) <= 180:
                    continue
                counts["title"] += 1
                language = language_of(title, source.language)
                if language is None:
                    continue
                counts["language"] += 1
                if not matches("style", title):
                    continue
                counts["brand"] += 1
                wanted = matches(key, title)
                if wanted:
                    counts["section"] += 1
                try:
                    at = datetime.fromisoformat(item["date"].replace("Z", "+00:00")).timestamp()
                    url = _link(item["link"], source)
                except (ValueError, OverflowError):
                    continue
                if not 0 <= now - at < MAX_AGE[key]:
                    continue
                if wanted:
                    counts["age"] += 1
                mark = fingerprint(title)
                if mark in seen:
                    continue
                if wanted:
                    counts["seen"] += 1
                if predicate is not None and not predicate(title):
                    continue
                if wanted:
                    counts["predicate"] += 1
                row["fallback_accepted"] += 1
                candidates.append(
                    {
                        "headline": title,
                        "source_id": source.id,
                        "source": source.name,
                        "source_url": url,
                        "published_at": at,
                        "published_label": published_label(at),
                        "fingerprint": mark,
                        "language": language,
                    }
                )
        except (NetworkError, OSError, ValueError) as error:
            row["reason"] = "FETCH_OR_PARSE_FAILED"
            row["error_type"] = type(error).__name__
            failures.append({"source_id": source.id, "error_type": type(error).__name__})
    desired = [item for item in candidates if matches(key, item["headline"])]
    effective = key if desired else "style"
    pool = desired or candidates
    if not pool:
        raise NewsUnavailable("� ���� ��� ��� ��� �� ���� ���.", diagnostics=diagnostics)
    selected = max(
        pool,
        key=lambda item: (
            item["language"] == "ar",
            top_brand("style", item["headline"]),
            item["published_at"],
        ),
    )
    content = {
        "schema": 1,
        "type": "news_review",
        "command": key,
        "fashion_desk": effective,
        "title": TITLES[effective],
        "sacred": False,
        "publication_enabled": False,
        "audio_review": AUDIO_POLICY,
        "media_policy": MEDIA_POLICY,
        **selected,
        "requires_owner_review": selected["language"] == "en",
        "fetched_at": now,
        "expires_at": min(now + 1800, selected["published_at"] + MAX_AGE[key]),
    }
    content["content_hash"] = digest(content)
    return {
        "content": content,
        "failures": failures,
        "diagnostics": diagnostics,
        "external_delivery": False,
    }
