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
        text = i["title"] + " " + i["detail"][:150]
        for other in OTHER_KINGDOMS:
            text = text.replace(other, " ")
        return SAUDI.search(text) is not None

    local = sorted((i for i in candidates if saudi(i)), key=lambda i: not trending(i))
    rest = sorted((i for i in candidates if not saudi(i)), key=lambda i: not trending(i))
    if key == "world":
        return country_cap(rest)
    # Owner 21 Nov 2026: «أخبار السعودية» carries Saudi stories only (Alaska, Iran and an
    # Egyptian singer had filled half of it under the Saudi badge), and no sport: «المفترض
    # الرياضه في اخبار الرياضه».
    if key == "saudi":
        return [i for i in local if not sport(i)]
    if key in {"news", "news_noon"}:
        # Owner 21 Nov 2026 («اعتمد»): the general bulletins leave sport to its command.
        local = [i for i in local if not sport(i)]
        rest = [i for i in rest if not sport(i)]
    return local[:2] + rest[:2] + local[2:] + rest[2:]


SAUDI_MARKERS = (
    "السعود",
    "الرياض",
    "المملكة",
    "جدة",
    "مكة",
    "المدينة المنورة",
    "ولي العهد السعودي",
    "محمد بن سلمان",
    "نيوم",
    "أرامكو",
    "دوري روشن",
    "رؤية 2030",
)
# Whole words, with a leading و/ف/ب/ل/ك allowed: «الرياض» never matches «الرياضيات» or
# «الرياضة» (a maths story had passed as Saudi). «السعود» keeps its endings (السعودية…).
SAUDI = re.compile(
    r"(?<![ء-ي])[وفبلك]?(?:"
    + "|".join(re.escape(w) + (r"[ء-ي]*" if w == "السعود" else "") for w in SAUDI_MARKERS)
    + r")(?![ء-ي])"
)
SPORT_WORDS = (
    "كرة القدم",
    "كرة الطاولة",
    "دوري",
    "الدوري",
    "دوري روشن",
    "منتخب",
    "مباراة",
    "مباريات",
    "لاعب",
    "لاعبين",
    "مدرب",
    "مهاجم",
    "حارس",
    "الهدافين",
    "نادي",
    "أولمبياد",
    "الأولمبية",
    "بطولة",
    "سباق",
    "سباقات",
    "جياد",
    "خيل",
    "الخيل",
    "شوط",
)
SPORT = re.compile(
    r"(?<![ء-ي])[وفبلك]?(?:ال|لل)?(?:"
    # رياضة/رياضي/رياضية/رياضات/رياضيين…, never the city «الرياض» or «الرياضيات» (maths).
    + r"رياض(?:ة|ه|ي|ية|يه|يا|يون|يين|ات)|"
    + "|".join(re.escape(w) for w in SPORT_WORDS)
    + r")(?![ء-ي])"
)


def sport_text(text: str) -> bool:
    """Any sport: the «أخبار الرياضة» command takes it, the general bulletins leave it."""
    from studio_core.sources.news import OTHER_SPORT, WORDS

    return (
        SPORT.search(text) is not None
        or OTHER_SPORT.search(text) is not None
        or any(word in text for word in WORDS["football"])
    )


def sport(item: dict[str, Any]) -> bool:
    return sport_text(item["title"] + " " + item["detail"][:150])


# «المملكة» alone means Saudi Arabia; these other kingdoms must not count.
OTHER_KINGDOMS = (
    "المملكة المتحدة",
    "المملكة الأردنية",
    "المملكة المغربية",
    "مملكة البحرين",
    "المملكة الهولندية",
)


# «العالم» تتنوّع: لا أكثر من خبرين من نفس الدولة في أول النشرة (owner 14 Nov 2026: أربعة
# أخبار كلها عن العراق). The country is the first one named in the headline, else the lead.
COUNTRIES: dict[str, tuple[str, ...]] = {
    "iraq": ("العراق", "عراقي", "بغداد", "كركوك", "البصرة", "أربيل"),
    "iran": ("إيران", "ايران", "إيراني", "طهران"),
    "usa": ("أمريكا", "أميركا", "الأمريكي", "الأميركي", "واشنطن", "ترامب", "الولايات المتحدة"),
    "russia": ("روسيا", "روسي", "موسكو", "الكرملين", "بوتين"),
    "ukraine": ("أوكرانيا", "أوكراني", "كييف", "زيلينسكي"),
    "israel": ("إسرائيل", "إسرائيلي", "تل أبيب", "نتنياهو"),
    "palestine": ("فلسطين", "فلسطيني", "غزة", "الضفة", "رام الله"),
    "lebanon": ("لبنان", "لبناني", "بيروت"),
    "syria": ("سوريا", "سوري", "دمشق"),
    "yemen": ("اليمن", "يمني", "صنعاء", "عدن", "الحوثي"),
    "egypt": ("مصر", "مصري", "القاهرة"),
    "sudan": ("السودان", "سوداني", "الخرطوم"),
    "turkey": ("تركيا", "تركي", "أنقرة", "إسطنبول", "أردوغان"),
    "china": ("الصين", "صيني", "بكين"),
    "uk": ("بريطانيا", "بريطاني", "لندن"),
    "france": ("فرنسا", "فرنسي", "باريس"),
    "germany": ("ألمانيا", "ألماني", "برلين"),
    "india": ("الهند", "هندي", "نيودلهي"),
    "pakistan": ("باكستان", "باكستاني", "إسلام آباد"),
    "uae": ("الإمارات", "إماراتي", "دبي", "أبوظبي"),
    "qatar": ("قطر", "قطري", "الدوحة"),
    "kuwait": ("الكويت", "كويتي"),
    "jordan": ("الأردن", "أردني", "عمّان"),
    "japan": ("اليابان", "ياباني", "طوكيو"),
}


def story_country(item: dict[str, Any]) -> str:
    for text in (item.get("title", ""), item.get("detail", "")[:200]):
        hits = [
            (min(text.find(w) for w in words if w in text), country)
            for country, words in COUNTRIES.items()
            if any(w in text for w in words)
        ]
        if hits:
            return min(hits)[1]
    return ""


def country_cap(items: list[dict[str, Any]], limit: int = 2) -> list[dict[str, Any]]:
    """Keep order, but stories beyond ``limit`` from one country wait at the end."""
    seen: dict[str, int] = {}
    first: list[dict[str, Any]] = []
    later: list[dict[str, Any]] = []
    for item in items:
        country = story_country(item)
        if country:
            seen[country] = seen.get(country, 0) + 1
            if seen[country] > limit:
                later.append(item)
                continue
        first.append(item)
    return first + later


SHARP_WIDTH = 480  # a news card picture is ~1000 px wide; smaller ones blur when stretched


def sharper(url: str) -> str:
    """The same picture at a larger size, for publishers whose feeds carry thumbnails.

    Sky News Arabia: /images/v1/Y/M/D/ID/W/H/… (feeds give 95/53 or 100/56); BBC: /ace/ws/W/.
    """
    sky = re.search(r"(images\.skynewsarabia\.com/images/v1/\d{4}/\d{2}/\d{2}/\d+/)(\d+)/(\d+)/", url)
    if sky and 0 < int(sky[2]) < 1000 and int(sky[3]) > 0:
        # Their pictures are 16:9; 1200/675 is the size checked against the site.
        ratio = int(sky[2]) / int(sky[3])
        high = 675 if abs(ratio - 16 / 9) < 0.08 else round(1200 / ratio)
        return url.replace(sky[0], f"{sky[1]}1200/{high}/", 1)
    bbc = re.search(r"(ichef\.bbci\.co\.uk/(?:ace/ws|news))/(\d+)/", url)
    if bbc and int(bbc[2]) < 1000:
        return url.replace(bbc[0], f"{bbc[1]}/1024/", 1)
    return url


class NewsPreparation(PreparationBase):
    def news_picture(self, item: dict[str, Any]) -> str:
        """The feed image, then the original article image, each tried large first.

        A sharp picture (at least SHARP_WIDTH wide) wins; only when none is found does the
        largest-available one stand in, so a story is never dropped for its picture size.
        """
        urls = [u for u in (item.get("image", ""), item.get("og_image", "")) if u]
        candidates = list(dict.fromkeys(c for u in urls for c in (sharper(u), u)))
        for minimum in (SHARP_WIDTH, 0):
            for url in candidates:
                try:
                    return self.picture(url, item["url"], item["source"], min_width=minimum)
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
            if source.id == "aawsat-fashion":
                section = unquote(urlsplit(source.url).path).rstrip("/") + "/"
                if not re.match(re.escape(section) + r"\d+-", unquote(urlsplit(link).path)):
                    continue
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

    # Economy section feeds; a markets story from any other feed needs an economic headline
    # (owner 12 Nov 2026: a nutrition story entered «الأسواق» through «فائدة» in its text).
    ECONOMY_FEEDS = frozenset(
        {"aawsat-economy", "bbc-ar-business", "skynews-ar-business", "asharq-business"}
    )

    @staticmethod
    def editorial_match(
        key: str, text: str, language: str, *, detail: str = "", source_id: str = ""
    ) -> bool:
        from studio_core.sources.news import (
            STYLE_EXCLUDED,
            football_story,
            matches,
            top_brand,
            world_story,
        )

        from .fashion_policy import approved_brand

        combined = text + " " + detail
        if key == "style" and approved_brand(combined):
            return not any(word in combined for word in STYLE_EXCLUDED) and not re.search(
                r"\b(?:bank|brokerage|efg)\b", combined, re.IGNORECASE
            )

        if language == "ar":
            if key == "football":
                return football_story(text, detail)
            if key == "sports":
                return sport_text(text + " " + detail[:150])
            if key == "world":
                return world_story(text, detail)
            if key == "markets" and source_id not in NewsPreparation.ECONOMY_FEEDS:
                return parsing.market_story(text) and matches(key, text + " " + detail)
            text = text + " " + detail
            return matches(key, text)
        text = text + " " + detail
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

    def news_candidates(self, key: str | None = None) -> list[dict[str, Any]]:
        from studio_core.sources.news import MAX_AGE, _link, news_sources, published_label

        key = key or ("news" if self.key == "news_noon" else self.key)
        from studio_core.sources.panel import approved as panel_sources

        sources = news_sources(key, production=True) + panel_sources(self.app, key)
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
                if source.id == "arriyadiyah":
                    from studio_core.sources.publisher_feeds import HOME, arriyadiyah

                    raw = arriyadiyah(raw.encode(), self.get(HOME).decode()).decode()
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
                        or not self.editorial_match(
                            key, title, source.language, detail=detail, source_id=source.id
                        )
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
        return candidates

    def news(self) -> list[dict[str, Any]]:
        from studio_core.production.news_selection import diverse, publisher

        key = "news" if self.key == "news_noon" else self.key
        candidates = self.news_candidates()
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
        publishers: dict[str, int] = {}
        # Snap bulletins carry one more story to pass the one-minute Spotlight floor.
        count = 1 if self.key in {"tech", "style"} else (4 if self.app == "snap-bot-v2" else 3)
        attempts = 0
        for item in diverse(candidates, publishers):
            topic = item["title"] + " " + item["detail"][:120]
            if self.key not in {"tech", "style"} and parsing.same_topic(topic, topics):
                continue
            if attempts >= 25:
                break
            attempts += 1
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
                                pic = self.picture(
                                    sharper(point["pic"]),
                                    item["url"],
                                    item["source"],
                                    min_width=SHARP_WIDTH,
                                )
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
                identity = publisher(item["url"])
                publishers[identity] = publishers.get(identity, 0) + 1
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
