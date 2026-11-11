"""NewsPreparation methods; behavior preserved from production.prepare.Factory."""

import html
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo

from . import parsing
from .model import (
    NEWS,
    canonical,
    scene,
    sha,
)
from .prepare_base import DIGITS, PreparationBase, safe_xml


def desk_order(key: str, candidates: list[dict[str, Any]], hot: list[str]) -> list[dict[str, Any]]:
    """Order a bulletin's stories; what Saudi Arabia is searching comes first in each group.

    status-bot newsroom.stories: two Saudi stories, two others, then the rest. «العالم» is
    the world only (owner 10 Nov 2026: «العالم عالمي فعلًا بلا الأخبار السعودية»).
    """

    def trending(i: dict[str, Any]) -> bool:
        text = (i["title"] + " " + i["detail"][:200]).casefold()
        return any(t.strip() and t.strip().casefold() in text for t in hot[:25])

    def saudi(i: dict[str, Any]) -> bool:
        return any(w in i["title"] + i["detail"][:150] for w in ("السعود", "الرياض", "المملكة"))

    local = sorted((i for i in candidates if saudi(i)), key=lambda i: not trending(i))
    rest = sorted((i for i in candidates if not saudi(i)), key=lambda i: not trending(i))
    if key == "world":
        return rest
    return local[:2] + rest[:2] + local[2:] + rest[2:]


class NewsPreparation(PreparationBase):
    def news_picture(self, item: dict[str, Any]) -> str:
        """Try the feed image, then the original article image."""
        for url in dict.fromkeys((item.get("image", ""), item.get("og_image", ""))):
            if not url:
                continue
            try:
                return self.picture(url, item["url"], item["source"])
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                continue
        raise ValueError("LEGACY_EXACT_STORY_IMAGE_UNAVAILABLE")

    def article(self, item: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        from studio_core.sources.news import _link, news_sources

        source = next(
            s for s in news_sources(self.key, production=True) if s.id == item["source_id"]
        )
        _link(item["url"], source)  # Reject feed-injected foreign publishers before fetching.
        page = item.pop("page", None) or self.get(item["url"]).decode("utf-8", "replace")
        text = parsing.body_text(page)
        og = re.search(
            r"<meta[^>]+(?:property|name)=[\"\']og:image[\"\'][^>]+content=[\"\']([^\"\']+)",
            page,
            re.I,
        ) or re.search(
            r"<meta[^>]+content=[\"\']([^\"\']+)[\"\'][^>]+(?:property|name)=[\"\']og:image",
            page,
            re.I,
        )
        item["og_image"] = html.unescape(og[1]) if og else ""
        item["image"] = item.get("image") or item["og_image"]
        self.sources.append(
            {
                "url": item["url"],
                "sha256": sha(page.encode()),
                "source_id": item["source_id"],
                "published": item["published"],
            }
        )
        # Keep the complete source snapshot locally. Scene points come only from its
        # headings/paragraphs, as the old factory's non-AI fallback does.
        item["article"] = self.save(page.encode(), ".html", item["url"], item["source"])
        return text, parsing.plain_points(text, item["detail"])

    def website_candidates(self, source: Any) -> list[dict[str, Any]]:
        home = self.get(source.url).decode("utf-8", "replace")
        feed = self.save(home.encode(), ".html", source.url, source.name)
        links = []
        host = urlsplit(source.url).hostname
        for href in re.findall(r'href=["\']([^"\']+)', home):
            link = urljoin(source.url, html.unescape(href)).split("#")[0]
            if (
                urlsplit(link).hostname != host
                or link in links
                or len(unquote(urlsplit(link).path)) < 40
                or any(
                    x in urlsplit(link).path
                    for x in ("/assets/", "/bundles/", "/files/", "/videos/")
                )
            ):
                continue
            links.append(link)
        candidates = []
        for link in links[:18]:
            try:
                page = self.get(link).decode("utf-8", "replace")
                title_match = re.search(
                    r'(?:property|name)=["\']og:title["\'][^>]+content=["\']([^"\']+)', page, re.I
                )
                date_match = re.search(r'"datePublished"\s*:\s*"([^"<>]+)"', page) or re.search(
                    r'property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)', page
                )
                if not title_match:
                    continue
                if date_match:
                    published = datetime.fromisoformat(
                        date_match[1].replace("Z", "+00:00")
                    ).timestamp()
                else:
                    day_match = re.search(
                        r'class=["\']section__date["\'][^>]*>(\d{2}\.\d{2}\.\d{4})<', page
                    )
                    if not day_match:
                        continue
                    published = (
                        datetime.strptime(day_match[1], "%d.%m.%Y")
                        .replace(tzinfo=ZoneInfo("Asia/Riyadh"))
                        .timestamp()
                    )
                if not 0 <= time.time() - published < 604800:
                    continue
                title = parsing.clean(title_match[1])
                text = parsing.words_only(parsing.body_text(page))
                if len(re.findall(r"[ء-ي]", text)) < 200 or self.used(link, reserve=False):
                    continue
                from studio_core.sources.news import matches, published_label

                if not matches("style", title + " " + text[:400]):
                    continue

                candidates.append(
                    {
                        "title": title,
                        "detail": text[:230],
                        "url": link,
                        "source": source.name,
                        "source_id": source.id,
                        "published": published,
                        "published_label": published_label(published),
                        "feed": feed,
                        "page": page,
                        "image": "",
                    }
                )
            except (OSError, ValueError, RuntimeError):
                continue
        return candidates

    @staticmethod
    def editorial_match(key: str, text: str, language: str) -> bool:
        from studio_core.sources.news import matches, top_brand

        if language == "ar":
            return matches(key, text)
        return key == "tech" or (
            key == "style"
            and (
                top_brand("style", text)
                or any(
                    w in text.casefold()
                    for w in (
                        "fashion",
                        "beauty",
                        "makeup",
                        "fragrance",
                        "perfume",
                        "runway",
                        "couture",
                        "celebrity",
                        "skincare",
                        "dress",
                    )
                )
            )
        )

    def news(self) -> list[dict[str, Any]]:
        from studio_core.sources.news import MAX_AGE, _link, news_sources, published_label

        key = "news" if self.key == "news_noon" else self.key
        from studio_core.sources.panel import approved as panel_sources

        sources = news_sources(self.key, production=True) + panel_sources(self.app, key)
        now = time.time()
        candidates: list[dict[str, Any]] = []
        for source in sources:
            if source.kind == "website":
                try:
                    candidates.extend(self.website_candidates(source))
                except (OSError, ValueError, RuntimeError):
                    continue
                continue
            try:
                raw = self.get(source.url).decode("utf-8", "replace")
                feed = self.save(raw.encode(), ".xml", source.url, source.name)
                raw = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", raw)
                for entry in safe_xml(raw.encode()).iter("item"):
                    title = parsing.clean(entry.findtext("title") or "")
                    url = (entry.findtext("link") or "").strip()
                    body = (
                        entry.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
                        or entry.findtext("description")
                        or ""
                    )
                    detail = parsing.clean(body)
                    try:
                        _link(url, source)
                        published = parsing.feed_time(entry.findtext("pubDate") or "")
                    except ValueError:
                        continue
                    if (
                        not title
                        or not url
                        or not 0 <= now - published < MAX_AGE[key]
                        or not self.editorial_match(key, title + " " + detail, source.language)
                    ):
                        continue
                    if self.used(url, reserve=False) or any(
                        x["title"] == title for x in candidates
                    ):
                        continue
                    candidates.append(
                        {
                            "title": title,
                            "detail": detail,
                            "url": url,
                            "image": parsing._img(entry),
                            "source": source.name,
                            "source_id": source.id,
                            "published": published,
                            "published_label": published_label(published),
                            "feed": feed,
                            "language": source.language,
                        }
                    )
            except (OSError, ValueError, KeyError, ET.ParseError, TypeError, RuntimeError):
                continue
        candidates.sort(key=lambda i: i["published"], reverse=True)
        if self.key == "style":
            from studio_core.sources.news import top_brand

            candidates.sort(
                key=lambda i: top_brand("style", i["title"] + " " + i["detail"]), reverse=True
            )
        if self.key == "urgent":
            candidates.sort(
                key=lambda i: parsing.urgency_score(
                    {
                        "title": i["title"],
                        "detail": i["detail"],
                        "published": datetime.fromtimestamp(
                            i["published"], ZoneInfo("Asia/Riyadh")
                        ),
                    }
                ),
                reverse=True,
            )
        elif self.key not in {"tech", "style"}:
            # Old bot's desk (owner decision 7 Nov): Saudi first, then Google Trends SA.
            # Trends only rank the publishers' stories; they never supply a story.
            try:
                trends = safe_xml(self.get("https://trends.google.com/trending/rss?geo=SA"))
                hot = [e.text or "" for e in trends.findall("./channel/item/title")]
            except (OSError, ValueError, ET.ParseError, RuntimeError):
                hot = []

            candidates = desk_order(self.key, candidates, hot)
        selected: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        topics: list[str] = []  # one story per event inside a bulletin
        # Snap bulletins carry one more story to pass the one-minute Spotlight floor.
        count = 1 if self.key in {"tech", "style"} else (4 if self.app == "snap-bot-v2" else 3)
        for item in candidates[:25]:
            topic = item["title"] + " " + item["detail"][:120]
            if self.key not in {"tech", "style"} and parsing.same_topic(topic, topics):
                continue
            try:
                text, points = self.article(item)
                if key == "markets" and parsing.market_excluded(parsing.words_only(text)):
                    continue
                if self.key in {"tech", "style"} and len(parsing.words_only(text)) < 300:
                    continue
                image = self.news_picture(item)
                if self.key in {"tech", "style"}:
                    if item.get("language") == "en":
                        original_title = item["title"]
                        item["title"] = self.translate(original_title)
                        item["translations"] = {"body": self.translations[original_title]}
                        points = [
                            {
                                **p,
                                "head": self.translate(p["head"]) if p["head"] else "",
                                "body": self.translate(p["body"]),
                                "translations": {
                                    field: self.translations[p[field]]
                                    for field in ("head", "body")
                                    if p[field]
                                },
                            }
                            for p in points
                        ]
                    # Exact extracted paragraphs with their matching article image.
                    selected.append(
                        scene(
                            item["title"],
                            max(5.0, len(item["title"]) / 17 + 0.25),
                            role="headline",
                            translations=item.get("translations", {}),
                            image=image,
                            image_credit=item["source"],
                            source=item["source"],
                            source_id=item["source_id"],
                            source_url=item["url"],
                            published=item["published"],
                            article=item["article"],
                            feed=item["feed"],
                            published_label=item["published_label"],
                        )
                    )
                    chosen = [p for p in points if p.get("body")][:4]
                    while (
                        len(chosen) > 2
                        and sum(
                            max(9.0, (len(p["body"]) + len(p["head"])) / 17 + 0.25) for p in chosen
                        )
                        > 44.7
                    ):
                        chosen.pop()
                    for point in chosen:
                        pic = image
                        if point.get("pic"):
                            try:
                                pic = self.picture(point["pic"], item["url"], item["source"])
                            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                                pic = image
                        selected.append(
                            scene(
                                point["body"],
                                max(9.0, (len(point["body"]) + len(point["head"])) / 17 + 0.25),
                                role="point",
                                translations=point.get("translations", {}),
                                head=point["head"],
                                image=pic,
                                image_credit=item["source"],
                                source=item["source"],
                                published_label=item["published_label"],
                            )
                        )
                    if len(selected) < 3:
                        selected.clear()
                        continue
                else:
                    detail = parsing.clean(
                        item["detail"] or parsing.words_only(text),
                        # Owner decision 3 Nov 2026 (old bot build_news): detail up to 230 letters
                        # with the title, from the source text; restored 10 Nov («انقلها للجديد»).
                        max(100, 230 - len(item["title"])),
                    )
                    if not detail or detail == item["title"]:
                        continue
                    selected.append(
                        scene(
                            detail,
                            max(12.0, (len(item["title"]) + len(detail)) / 17 + 0.25),
                            role="story",
                            head=item["title"],
                            image=image,
                            image_credit=item["source"],
                            source=item["source"],
                            source_id=item["source_id"],
                            source_url=item["url"],
                            published=item["published"],
                            article=item["article"],
                            feed=item["feed"],
                            published_label=item["published_label"],
                            conflict=parsing.conflict_label(
                                {"title": item["title"], "detail": detail}
                            ),
                        )
                    )
                for selected_scene in selected:
                    if "source_id" not in selected_scene:
                        selected_scene.update(
                            {
                                k: item[k]
                                for k in ("source_id", "url", "published", "article", "feed")
                            }
                        )
                        selected_scene["source_url"] = item["url"]
                self.used(item["url"])
                topics.append(topic)
                if self.key in {"tech", "style"} or len(selected) >= count:
                    break
            except (
                OSError,
                ValueError,
                KeyError,
                StopIteration,
                RuntimeError,
                subprocess.SubprocessError,
            ) as error:
                failures.append(
                    {
                        "source": item["source_id"],
                        "code": type(error).__name__ + ":" + str(error)[:100],
                    }
                )
                continue
        if (
            self.key not in {"tech", "style"} and len(selected) < (3 if self.key == "urgent" else 2)
        ) or not selected:
            (self.root / "source-failures.json").write_bytes(canonical(failures))
            if self.key == "urgent":
                from studio_core.observability.failure_code import NoFreshContent

                raise NoFreshContent()
            raise ValueError("LEGACY_NEWS_WITH_IMAGES_UNAVAILABLE")
        if self.key in NEWS:
            for n, item in enumerate(selected, 1):
                item["number"] = f"{n} / {len(selected)}".translate(DIGITS)
        return selected
