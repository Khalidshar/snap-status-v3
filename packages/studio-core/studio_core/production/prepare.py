# ruff: noqa: E501
"""Source factory port: all downloads go through the application's source broker."""

import array
import email.utils
import html
import json
import math
import os
import re
import sqlite3
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from studio_core.config import Config
from studio_core.network import NetworkClient
from studio_core.network.production import ProductionPolicy
from studio_core.renderer import run
from studio_core.weather import content_hash

from . import parsing
from .model import (
    DATA,
    NEWS,
    QURAN,
    ROOT,
    SACRED,
    TITLES,
    budget,
    canonical,
    scene,
    sha,
    timed,
    validate,
)

DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
MUSIC = "https://www.free-stock-music.com/music/maxkomusic/mp3/maxkomusic-true-patriot.mp3"
MUSIC_CREDIT = "True Patriot by MaxKoMusic | https://maxkomusic.com/\nRoyalty Free Music by https://www.free-stock-music.com\nCC BY-SA 3.0 https://creativecommons.org/licenses/by-sa/3.0/"


def duration(path: Path) -> float:
    result = run(
        [
            "/usr/bin/ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        30,
    )
    value = float(result.stdout.strip())
    if not math.isfinite(value) or value <= 0:
        raise ValueError("LEGACY_AUDIO_DURATION_INVALID")
    return value


class Factory:
    def __init__(
        self,
        app: str,
        key: str,
        job: str,
        root: Path,
        *,
        client: Any = None,
        state: Path | None = None,
    ) -> None:
        if (
            app not in {"snap-bot-v2", "status-bot-v2"}
            or key not in TITLES
            or not re.fullmatch("[a-f0-9]{32}", job)
        ):
            raise ValueError("LEGACY_FACTORY_SCOPE")
        self.app, self.key, self.job, self.root = app, key, job, root
        short = "snap" if app == "snap-bot-v2" else "status"
        self.client = client or NetworkClient(
            cast(Config, ProductionPolicy(Path(f"/run/madar/{short}-production.sock")))
        )
        self.assets: list[dict[str, Any]] = []
        self.audio: list[dict[str, Any]] = []
        self.sources: list[dict[str, Any]] = []
        self.translations: dict[str, str] = {}
        self.root.mkdir(parents=True, exist_ok=False, mode=0o700)
        (root / "assets").mkdir(mode=0o750)
        folder = state or Path(
            os.getenv("MADAR_PRODUCTION_STATE_ROOT", f"/var/lib/madar/{short}/production")
        )
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = folder / "history.sqlite"
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE IF NOT EXISTS scope(app TEXT PRIMARY KEY)")
            rows = list(db.execute("SELECT app FROM scope"))
            if rows and rows != [(app,)]:
                raise ValueError("LEGACY_HISTORY_SCOPE")
            db.execute("INSERT OR IGNORE INTO scope VALUES(?)", (app,))
            db.execute(
                "CREATE TABLE IF NOT EXISTS used(kind TEXT, fingerprint TEXT, job TEXT, at REAL, PRIMARY KEY(kind,fingerprint))"
            )
            db.execute("CREATE TABLE IF NOT EXISTS readers(job TEXT PRIMARY KEY, id TEXT, at REAL)")

    def get(self, url: str) -> bytes:
        response = self.client.get(quote(url, safe=":/?&=+%-._~"))
        if response.status != 200:
            raise ValueError("LEGACY_SOURCE_UNAVAILABLE")
        return bytes(response.body)

    def json(self, url: str) -> Any:
        raw = self.get(url)
        self.save(raw, ".json", url, "source response")
        return json.loads(
            raw.decode("utf-8-sig"),
            strict=urlsplit(url).hostname not in {"hisnmuslim.com", "www.hisnmuslim.com"},
        )

    def save(self, raw: bytes, suffix: str, url: str, credit: str) -> str:
        name = f"assets/{len(self.assets):03d}{suffix}"
        path = self.root / name
        path.write_bytes(raw)
        path.chmod(0o640)
        self.assets.append({"file": name, "sha256": sha(raw), "source_url": url, "credit": credit})
        return name

    def local(self, path: Path, source: str, credit: str) -> str:
        return self.save(path.read_bytes(), path.suffix, source, credit)

    def used(self, fingerprint: str, *, reserve: bool = True) -> bool:
        window = 4 * 86400 if self.key in NEWS | {"tech", "style"} else 30 * 86400
        with sqlite3.connect(self.db) as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT job,at FROM used WHERE kind=? AND fingerprint=?", (self.key, fingerprint)
            ).fetchone()
            if old and old[0] != self.job and old[1] > time.time() - window:
                return True
            if reserve:
                db.execute(
                    "INSERT OR REPLACE INTO used VALUES(?,?,?,?)",
                    (self.key, fingerprint, self.job, time.time()),
                )
        return False

    def picture(self, url: str, base: str, credit: str) -> str:
        url = urljoin(base, html.unescape(url))
        raw = self.get(url)
        if not (
            raw.startswith(b"\xff\xd8")
            or raw.startswith(b"\x89PNG")
            or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")
        ):
            raise ValueError("LEGACY_IMAGE_TYPE_INVALID")
        original = self.root / "source-image.img"
        original.write_bytes(raw)
        normalized = self.root / "image.jpg"
        run(
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(original),
                "-frames:v",
                "1",
                "-vf",
                "scale=1200:1200:force_original_aspect_ratio=decrease",
                str(normalized),
            ],
            45,
        )
        return self.local(normalized, url, credit)

    def translate(self, text: str) -> str:
        from .translation import decode, request_url

        if not text:
            return text
        if self.key not in {"tech", "style"}:
            raise ValueError("TRANSLATION_SACRED_OR_NEWS_FORBIDDEN")
        url = request_url(text)
        raw = self.get(url)
        target = decode(text, raw)
        proof = self.save(raw, ".json", url, "ترجمة آلية مباشرة من النص الإنجليزي الأصلي")
        self.translations[text] = proof
        return target

    def news_picture(self, item: dict[str, Any]) -> str:
        """Use source images first; Google discovery must match the exact headline."""
        for url in dict.fromkeys((item.get("image", ""), item.get("og_image", ""))):
            if not url:
                continue
            try:
                return self.picture(url, item["url"], item["source"])
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                continue
        expected_title = item.get("original_title", item["title"])
        url = "https://news.google.com/rss/search?" + urlencode(
            {"q": '"' + expected_title + '"', "hl": "ar", "gl": "SA", "ceid": "SA:ar"}
        )
        raw = self.get(url)
        self.save(raw, ".xml", url, "Google News — اكتشاف صورة الخبر")
        for entry in safe_xml(raw).iter("item"):
            title = parsing.clean(entry.findtext("title") or "")
            source = parsing.clean(entry.findtext("source") or "")
            if title.removesuffix(" - " + source) != expected_title:
                continue
            publisher = entry.find("source")
            publisher_host = (
                urlsplit(publisher.get("url", "") if publisher is not None else "").hostname or ""
            )
            article_host = urlsplit(item["url"]).hostname or ""
            if source != item["source"] and publisher_host.removeprefix(
                "www."
            ) != article_host.removeprefix("www."):
                continue
            image = parsing._img(entry)
            try:
                if image:
                    return self.picture(image, item["url"], item["source"])
                link = (entry.findtext("link") or "").strip()
                if link:
                    page = self.get(link).decode("utf-8", "replace")
                    match = re.search(
                        r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                        page,
                        re.I,
                    )
                    if match:
                        return self.picture(match[1], link, source or item["source"])
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                continue
        raise ValueError("LEGACY_EXACT_STORY_IMAGE_UNAVAILABLE")

    def article(self, item: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        from studio_core.sources import for_command
        from studio_core.sources.news import _link

        source = next(
            s
            for s in for_command("news" if self.key == "news_noon" else self.key)
            if s.id == item["source_id"]
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
        from studio_core.sources import for_command
        from studio_core.sources.news import MAX_AGE, published_label

        key = "news" if self.key == "news_noon" else self.key
        now = time.time()
        candidates: list[dict[str, Any]] = []
        for source in for_command(key):
            if (
                source.status != "approved"
                or (
                    source.language != "ar"
                    and not (self.key in {"tech", "style"} and source.language == "en")
                )
                or source.kind != "rss"
            ):
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
                    published = email.utils.parsedate_to_datetime(
                        entry.findtext("pubDate") or ""
                    ).timestamp()
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
        if self.key == "style":
            for source in for_command("style"):
                if (
                    source.status == "approved"
                    and source.language == "ar"
                    and source.kind == "website"
                ):
                    try:
                        candidates.extend(self.website_candidates(source))
                    except (OSError, ValueError, RuntimeError):
                        continue
        candidates = [c for c in candidates if urlsplit(c["url"]).hostname != "news.google.com"]
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
            # Same Saudi-first desk; trend RSS is optional and cannot manufacture a story.
            try:
                trends = safe_xml(self.get("https://trends.google.com/trending/rss?geo=SA"))
                hot = [e.text or "" for e in trends.findall("./channel/item/title")]
            except (OSError, ValueError, ET.ParseError, RuntimeError):
                hot = []
            candidates.sort(
                key=lambda i: (
                    not any(w in i["title"] + i["detail"] for w in ("السعود", "الرياض", "المملكة")),
                    not any(w in i["title"] for w in hot),
                )
            )
        selected: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        # Snap bulletins carry one more story to pass the one-minute Spotlight floor.
        count = 1 if self.key in {"tech", "style"} else (4 if self.app == "snap-bot-v2" else 3)
        for item in candidates[:25]:
            try:
                text, points = self.article(item)
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
                        min(115, max(70, 200 - len(item["title"]))),
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
            raise ValueError("LEGACY_NEWS_WITH_IMAGES_UNAVAILABLE")
        if self.key in NEWS:
            for n, item in enumerate(selected, 1):
                item["number"] = f"{n} / {len(selected)}".translate(DIGITS)
        return selected

    def quran(
        self, surah: int, ayahs: list[int], *, background: bool = False
    ) -> list[dict[str, Any]]:
        from studio_core.religious.quran_foundation import exact_ayah

        catalog = json.loads((ROOT / "config/production/reciters.json").read_text())
        readers = catalog["reciters"]
        if self.key in {"isha", "night"}:
            readers = [r for r in readers if r["id"] in catalog["calm"]]
        with sqlite3.connect(self.db) as db:
            last = db.execute("SELECT id FROM readers ORDER BY at DESC LIMIT 1").fetchone()
        last_name = next(
            (r["name"] for r in catalog["reciters"] if last and r["id"] == last[0]),
            last[0] if last else None,
        )
        pos = next((i for i, r in enumerate(readers) if r["name"] == last_name), -1)
        order = readers[pos + 1 :] + readers[: pos + 1]
        mp3 = [r for r in order if r["provider"] == "mp3quran"]
        fallback = [r for r in order if r["provider"] == "everyayah"]
        try:
            reads = self.json("https://mp3quran.net/api/v3/ayat_timing/reads")
        except (OSError, ValueError, RuntimeError):
            reads = []
        for reader in mp3 + fallback:
            if reader["name"] == last_name:
                continue
            try:
                recordings = []
                # Fail locally before downloading if an official reference is absent.
                for a in ayahs:
                    exact_ayah(surah, a)
                if reader["provider"] == "mp3quran":
                    read = next(r for r in reads if str(r["id"]) == reader["id"].split(":")[1])
                    timings = self.json(
                        f"https://mp3quran.net/api/v3/ayat_timing?surah={surah}&read={read['id']}"
                    )
                    url = f"{read['folder_url'].replace('http://', 'https://').rstrip('/')}/{surah:03d}.mp3"
                    full = self.root / "surah.mp3"
                    full.write_bytes(self.get(url))
                for ayah in ayahs:
                    audio = self.root / f"ayah-{ayah}.mp3"
                    if reader["provider"] == "mp3quran":
                        row = next(r for r in timings if r["ayah"] == ayah)
                        start, end = float(row["start_time"]) / 1000, float(row["end_time"]) / 1000
                        if not math.isfinite(start + end) or start < 0 or end <= start:
                            raise ValueError("LEGACY_AYAH_TIMING_INVALID")
                        run(
                            [
                                "/usr/bin/ffmpeg",
                                "-v",
                                "error",
                                "-nostdin",
                                "-y",
                                "-protocol_whitelist",
                                "file,pipe",
                                "-i",
                                str(full),
                                "-ss",
                                str(max(0, start - 0.05)),
                                "-t",
                                str(end - start + 0.3),
                                "-ar",
                                "48000",
                                "-ac",
                                "2",
                                str(audio),
                            ],
                            180,
                        )
                    else:
                        url = f"https://everyayah.com/data/{reader['id']}/{surah:03d}{ayah:03d}.mp3"
                        audio.write_bytes(self.get(url))
                    recordings.append(
                        self.recitation_scene(
                            surah,
                            ayah,
                            audio,
                            url,
                            reader["name"],
                            "mp3quran" if reader["provider"] == "mp3quran" else "EveryAyah",
                            background=background,
                        )
                    )
                if sum(x["seconds"] for x in recordings) > 50:
                    raise ValueError("LEGACY_WHOLE_PASSAGE_EXCEEDS_58_SECONDS")
                self.remember_reader(reader["name"])
                return recordings
            except (
                OSError,
                ValueError,
                KeyError,
                StopIteration,
                RuntimeError,
                subprocess.SubprocessError,
            ):
                continue
        extra = self.foundation_recitation(surah, ayahs, last_name, background=background)
        if extra:
            return extra
        raise ValueError("LEGACY_HUMAN_RECITATION_UNAVAILABLE")

    def remember_reader(self, name: str) -> None:
        with sqlite3.connect(self.db, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT job,id FROM readers ORDER BY at DESC LIMIT 1").fetchone()
            if current and current[0] != self.job and current[1] == name:
                raise ValueError("LEGACY_READER_REPEATED")
            db.execute(
                "INSERT OR REPLACE INTO readers VALUES(?,?,?)", (self.job, name, time.time())
            )

    def recitation_scene(
        self,
        surah: int,
        ayah: int,
        audio: Path,
        url: str,
        name: str,
        provider: str,
        *,
        background: bool,
    ) -> dict[str, Any]:
        from studio_core.religious.quran_foundation import exact_ayah

        seconds = duration(audio)
        run(
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-nostdin",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(audio),
                "-f",
                "null",
                "-",
            ],
            180,
        )
        result = self.json(
            f"https://quranenc.com/api/v1/translation/aya/arabic_moyassar/{surah}/{ayah}"
        )["result"]
        if int(result["sura"]) != surah or int(result["aya"]) != ayah:
            raise ValueError("LEGACY_TAFSIR_REFERENCE_CHANGED")
        tafsir = html.unescape(re.sub("<[^>]*>", "", result["translation"]))
        text = exact_ayah(surah, ayah)
        hold = seconds + 0.8 if background else seconds + 0.8 + max(len(tafsir) / 17 + 1, 5.0)
        return scene(
            text,
            hold,
            role="verse",
            verse=[surah, ayah],
            tafsir=tafsir,
            tafsir_sha256=sha(tafsir.encode()),
            source=f"بصوت {name} — التلاوة: {provider}",
            audio=self.local(audio, url, name),
            audio_duration=seconds,
        )

    def foundation_recitation(
        self, surah: int, ayahs: list[int], last_name: str | None, *, background: bool
    ) -> list[dict[str, Any]]:
        # Keys stay exclusively in the already configured QF broker. Missing service
        # never blocks the two keyless providers above, and never triggers TTS.
        if os.environ.get("MADAR_QURAN_AUDIO") != "1":
            return []
        from studio_core.religious.quran_commands import QuranSocketPolicy
        from studio_core.religious.quran_foundation import CATALOG, Source
        from studio_core.religious.quran_pipeline import store_for

        short = "snap" if self.app == "snap-bot-v2" else "status"
        client = NetworkClient(
            cast(Config, QuranSocketPolicy(Path(f"/run/madar/{short}-quran.sock")))
        )
        try:
            ready = json.loads(client.get("quran://ready").body)
            if ready.get("configured") is not True:
                return []
            source = Source(client, ready["environment"])
            choices = source.catalog(json.loads(CATALOG.read_bytes()))["reciters"]
            store = store_for(self.app)
            store.sweep()
            for reader in choices:
                if reader["name"] == last_name:
                    continue
                qf_job = sha((self.job + ":qf:" + str(reader["id"])).encode())[:32]
                _, expires = store.reserve(qf_job, {"reciters": [reader]})
                try:
                    scenes = []
                    for ayah in ayahs:
                        store.sweep()
                        audio = store.directory(qf_job) / f"{surah}-{ayah}.mp3"
                        audio.write_bytes(source.audio(f"{surah}:{ayah}", reader))
                        item = self.recitation_scene(
                            surah,
                            ayah,
                            audio,
                            "quran-foundation:" + str(reader["id"]) + f"/{surah}/{ayah}",
                            reader["name"],
                            "مؤسسة القرآن",
                            background=background,
                        )
                        asset = next(a for a in self.assets if a["file"] == item["audio"])
                        asset.update({"qf_job": qf_job, "expires_at": expires})
                        store.track(self.root / item["audio"], qf_job, expires, asset["sha256"])
                        scenes.append(item)
                    if sum(s["seconds"] for s in scenes) > 50:
                        raise ValueError("LEGACY_WHOLE_PASSAGE_EXCEEDS_58_SECONDS")
                    self.remember_reader(reader["name"])
                    return scenes
                except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
                    store.failed(qf_job)
                    continue
        except (OSError, ValueError, KeyError, RuntimeError):
            return []
        return []

    @staticmethod
    def audio_levels(path: Path) -> list[float]:
        raw = subprocess.run(  # noqa: S603 — fixed local-only decoder argv
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(path),
                "-ar",
                "6000",
                "-ac",
                "1",
                "-f",
                "s16le",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=120,
        ).stdout
        samples = array.array("h", raw)
        return [
            min(
                1.0,
                math.sqrt(sum(v * v for v in samples[i : i + 400]) / len(samples[i : i + 400]))
                / 8000,
            )
            for i in range(0, len(samples), 400)
        ]

    def adhkar(
        self, period: str, *, minimum: float = 44, maximum: float = 50
    ) -> list[dict[str, Any]]:
        """One official human recording per exact dhikr; no guessed archive timings."""
        chapter = 28 if period == "night" else 27
        url = f"https://www.hisnmuslim.com/api/ar/{chapter}.json"
        response = self.json(url)
        rows = next(iter(response.values()))
        selected: list[dict[str, Any]] = []
        total = 0.0
        for row in rows:
            text = row["ARABIC_TEXT"]
            # Quran is admitted exclusively from King Fahd, never this API. Skip
            # records with variants/instructions: their audio is not a single text.
            if any(token in text for token in ("\ufd3f", "[", "إذا", "أصبح", "أمسى", "أمسيت")):
                continue
            audio_url = row["AUDIO"].replace("http://", "https://", 1)
            if not re.fullmatch(r"https://www\.hisnmuslim\.com/audio/ar/[0-9]+\.mp3", audio_url):
                raise ValueError("LEGACY_DHIKR_AUDIO_SOURCE_INVALID")
            try:
                audio = self.save(self.get(audio_url), ".mp3", audio_url, "حصن المسلم — تسجيل بشري")
                seconds = duration(self.root / audio)
                if total + seconds + 0.3 > maximum:
                    continue
                selected.append(
                    scene(
                        text,
                        seconds + 0.3,
                        role="adhkar",
                        source="حصن المسلم",
                        number="التكرار: " + str(row["REPEAT"]).translate(DIGITS),
                        dhikr_id=row["ID"],
                        dhikr_chapter=chapter,
                        levels=self.audio_levels(self.root / audio),
                        audio=audio,
                        audio_duration=seconds,
                    )
                )
                total += seconds + 0.3
                if total >= minimum:
                    return selected
                if len(selected) >= 16:
                    break
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                continue
        raise ValueError("LEGACY_MATCHED_HUMAN_ADHKAR_UNAVAILABLE")

    def spoken(self, text: str, kind: str) -> tuple[str, float]:
        url = "tts://" + kind + "?" + urlencode({"text": text})
        path = self.save(
            self.get(url), ".mp3", url, "Microsoft Edge — ar-SA-HamedNeural (old free voice)"
        )
        return path, duration(self.root / path)

    def history(self, day: date) -> list[dict[str, Any]]:
        """حدث اليوم: Islamic-history events on today's Hijri date (owner decision 3 Nov).

        Source: the Arabic Wikipedia page of the Hijri day; Saudi and early Islamic first,
        nothing from the last 30 Hijri years, no killing or massacres.
        """
        hijri = self.json(f"https://api.aladhan.com/v1/gToH/{day:%d-%m-%Y}")["data"]["hijri"]
        month = re.sub(r"[ً-ْ]", "", hijri["month"]["ar"])
        page = f"{int(hijri['day'])} {month}"
        wiki = self.json(
            "https://ar.wikipedia.org/w/api.php?"
            + urlencode(
                {
                    "action": "parse",
                    "page": page,
                    "prop": "wikitext",
                    "format": "json",
                    "redirects": 1,
                }
            )
        )["parse"]["wikitext"]["*"]
        avoid = ("اغتيال", "مجزرة", "مذبحة", "إعدام", "انتحار", "تفجير", "قتل", "مقتل", "اغتصاب")
        saudi = ("السعودية", "عبد العزيز", "الحجاز", "نجد", "مكة", "المدينة المنورة", "آل سعود")
        latest = int(hijri["year"]) - 30
        events = []
        for line in wiki.splitlines():
            found = re.match(r"\*\s*(\d{1,4})\s*هـ\s*[^\w\s]\s*(.+)", line)
            if not found or int(found[1]) > latest:
                continue
            raw = re.sub(r"<ref[^>]*/>|<ref.*?</ref>|\{\{.*?\}\}", "", found[2])
            link = re.search(r"\[\[([^|\]#]+)", raw)
            text = parsing.clean(re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"", raw), 220)
            if len(text) < 20 or any(word in text for word in avoid):
                continue
            rank = (0 if any(word in text for word in saudi) else 1, int(found[1]))
            events.append((rank, int(found[1]), text, link[1] if link else ""))
        events.sort()
        need = 4 if self.app == "snap-bot-v2" else 3
        if len(events) < 2:
            raise ValueError("LEGACY_HISTORY_UNAVAILABLE")
        scenes = []
        for n, (_, year, text, link) in enumerate(events[:need], 1):
            extra: dict[str, Any] = {}
            if link:
                try:
                    summary = self.json(
                        "https://ar.wikipedia.org/api/rest_v1/page/summary/" + quote(link)
                    )
                    thumb = summary.get("originalimage") or summary.get("thumbnail") or {}
                    if thumb.get("source"):
                        extra = {
                            "image": self.picture(thumb["source"], thumb["source"], "ويكيبيديا"),
                            "image_credit": "ويكيبيديا",
                        }
                except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
                    extra = {}
            scenes.append(
                scene(
                    text,
                    max(9.0, len(text) / 15 + 3),
                    role="history",
                    head="في مثل هذا اليوم • سنة " + str(year).translate(DIGITS) + "هـ",
                    number=f"{n}/{min(need, len(events))}".translate(DIGITS),
                    source="ويكيبيديا العربية • " + page,
                    **extra,
                )
            )
        return scenes

    def variety(self) -> list[dict[str, Any]]:
        key = "facts" if self.key == "did_you_know" else self.key
        items = json.loads(
            (DATA / ("riddles.json" if key == "riddle" else f"{key}.json")).read_text()
        )
        need = 3 if key == "riddle" else 4
        selected = []
        for item in items:
            identity = sha(canonical(item))
            if self.used(identity, reserve=False):
                continue
            selected.append(item)
            self.used(identity)
            if len(selected) == need:
                break
        if len(selected) != need:
            raise ValueError("LEGACY_CATALOG_EXHAUSTED")
        scenes = []
        for n, item in enumerate(selected, 1):
            number = str(n).translate(DIGITS)
            if key == "riddle":
                spoken, length = self.spoken(item["a"], "riddle")
                scenes += [
                    scene(
                        item["q"],
                        8.2,
                        role="question",
                        number="لغز " + number,
                        countdown=5,
                        countdown_after=3.0,
                    ),
                    scene(
                        item["a"],
                        max(3.6, length + 0.3),
                        role="answer",
                        head=item["q"],
                        audio=spoken,
                        audio_duration=length,
                        voice_kind="speech",
                    ),
                ]
            elif key == "funny":
                scenes.append(
                    scene(
                        item["a"],
                        8.5,
                        role="funny",
                        punch=item["b"],
                        reveal_after=3.5,
                        number=number,
                    )
                )
            else:
                scenes.append(scene(str(item), 8.0, role="fact", number=number))
        return scenes

    def build(self) -> dict[str, Any]:
        key = self.key
        day = datetime.now(ZoneInfo("Asia/Riyadh")).date()
        night_number = max(1, (day - date(2026, 9, 26)).days + 1) if key == "night" else None
        title = (
            (TITLES[key] + " • الليلة " + str(night_number).translate(DIGITS))
            if night_number
            else TITLES[key]
        )
        scenes = [scene(title, 3.2, role="hook")]
        if key in NEWS | {"tech", "style"}:
            scenes += self.news()
        elif key in {"facts", "did_you_know", "riddle", "funny"}:
            scenes += self.variety()
        elif key == "history":
            scenes += self.history(day)
        elif key in QURAN:
            # Approved local King Fahd references; no fetched or normalised Quran text.
            data = json.loads((DATA / "status.json").read_bytes())
            pool = {
                "verse": "verses",
                "friday": "friday_verses",
                "kahf": "kahf_verses",
                "fast": "fast_verses",
                "isha": "isha_verses",
            }.get(key)
            candidates = [{"surah": s, "ayahs": a} for s, a, _ in data[pool]] if pool else []
            if key == "dua":
                candidates = [
                    {"surah": s, "ayahs": a}
                    for s, a in [
                        (2, [201]),
                        (3, [8]),
                        (3, [16]),
                        (7, [23]),
                        (14, [41]),
                        (20, [25, 26, 27, 28]),
                        (25, [74]),
                        (59, [10]),
                    ]
                ]
            for item in candidates:
                try:
                    scenes += self.quran(item["surah"], item["ayahs"])
                    break
                except ValueError:
                    continue
            else:
                raise ValueError("LEGACY_NO_WHOLE_PASSAGE_WITHIN_BUDGET")
        elif key in {"adhkar_am", "adhkar_pm"}:
            scenes += self.adhkar("am" if key.endswith("am") else "pm")
        elif key == "hadith":
            from studio_core.presentation import faith

            item = min(
                (i for i in faith.load("hadith") if i["grade"] == "صحيح"),
                key=lambda i: len(i["text"]),
            )
            spoken, length = self.spoken(item["text"], "hadith")
            scenes.append(
                scene(
                    item["text"],
                    max(24.0, length + 0.3),
                    role="hadith",
                    source=item["source"] + " • " + item["grade"],
                    hadith_id=item["id"],
                    grade=item["grade"],
                    audio=spoken,
                    audio_duration=length,
                    voice_kind="speech",
                )
            )
        elif key == "night":
            from studio_core.presentation import faith

            scenes += self.quran(94, [5, 6])
            item = min(
                (i for i in faith.load("hadith") if i["grade"] == "صحيح"),
                key=lambda i: len(i["text"]),
            )
            spoken, length = self.spoken(item["text"], "hadith")
            scenes.append(
                scene(
                    item["text"],
                    max(6.0, length + 0.3, len(item["text"]) / 17),
                    audio=spoken,
                    audio_duration=length,
                    voice_kind="speech",
                    role="hadith",
                    source=item["source"] + " • " + item["grade"],
                    hadith_id=item["id"],
                    grade=item["grade"],
                )
            )
            remaining = budget(self.app)[1] - 0.3 - 5.0 - sum(s["seconds"] for s in scenes)
            scenes += self.adhkar("night", minimum=8, maximum=min(24, remaining))
        elif key == "brief":
            scenes += self.brief()
        if key in NEWS | {"tech", "style"}:
            # Open with the actual first story and its image, without a title-only delay.
            scenes.pop(0)
            scenes[0]["bulletin_title"] = TITLES[key]
        credits = " • ".join(dict.fromkeys(s.get("source", "") for s in scenes if s.get("source")))
        scenes.append(
            scene(
                "تابعني للمزيد",
                4.5 if key in {"adhkar_am", "adhkar_pm"} else 5.0,
                role="cta",
                source=credits or "روّق مع خالد",
            )
        )
        total = sum(x["seconds"] for x in scenes)
        low, high, _ = budget(self.app)
        if total < low:
            scenes[-2]["seconds"] += low - total
        if total > high and key in NEWS:
            per = (high - 1.0 - 8.2) / (len(scenes) - 2)
            for item in scenes[1:-1]:
                item["seconds"] = per
        scenes, total = timed(scenes)
        for item in scenes:
            if item.get("audio"):
                self.audio.append(
                    {
                        "file": item["audio"],
                        "at": item["at"] + 0.15,
                        "seconds": item["audio_duration"],
                        "kind": item.get("voice_kind", "human"),
                    }
                )
        if key == "brief":
            self.audio = self.brief_audio
        music = None
        if key not in SACRED:
            music = self.save(self.get(MUSIC), ".mp3", MUSIC, MUSIC_CREDIT)
            duration(self.root / music)
        p = {
            "schema": 1,
            "app": self.app,
            "key": key,
            "scenes": scenes,
            "duration": total,
            "assets": self.assets,
            "audio": self.audio,
            "music": music,
            "effects": key not in SACRED,
            "sources": self.sources,
            **(
                {"night_number": night_number, "night_date": day.isoformat()}
                if night_number
                else {}
            ),
        }
        value = {
            "type": "did_you_know",
            "kind": key,
            "sacred": key in SACRED,
            "language": "ar",
            "title": TITLES[key],
            "body": "\n".join(s["body"] for s in scenes),
            "fact_id": "legacy-" + sha(canonical(p))[:32],
            "category": key,
            "source": "legacy-production",
            "source_display": "إنتاج البوت القديم",
            "source_url": "",
            "verified_at": "2026-11-03",
            "attribution": "روّق مع خالد",
            "footer": "تابعني للمزيد 👻",
            "fetched_at": datetime.now(UTC).isoformat(),
            "expires_at": time.time() + 3600,
            "legacy_production": p,
        }
        value["content_hash"] = content_hash(value)
        validate(value, assets=self.root)
        (self.root / "content.json").write_bytes(canonical(value))
        return value

    def brief(self) -> list[dict[str, Any]]:
        data = json.loads((DATA / "status.json").read_text())
        day = datetime.now(ZoneInfo("Asia/Riyadh"))
        info = self.json(
            f"https://api.aladhan.com/v1/timingsByCity/{day:%d-%m-%Y}?city=Riyadh&country=SA&method=4"
        )["data"]
        labels = {
            "Fajr": "الفجر",
            "Dhuhr": "الظهر",
            "Asr": "العصر",
            "Maghrib": "المغرب",
            "Isha": "العشاء",
        }

        def clock(value: str) -> str:
            h, m = map(int, value[:5].split(":"))
            return f"{h % 12 or 12}:{m:02d} {'ص' if h < 12 else 'م'}"

        prayers = "\n".join(
            f"{v} {clock(info['timings'][k])}" for k, v in labels.items()
        ).translate(DIGITS)
        events = []
        for name, m, d in [("اليوم الوطني", 9, 23), ("يوم التأسيس", 2, 22)]:
            at = day.date().replace(month=m, day=d)
            if at <= day.date():
                at = at.replace(year=at.year + 1)
            events.append((at, name))
        hy = int(info["date"]["hijri"]["year"])
        for name, hd, hm in [("رمضان", 1, 9), ("عيد الفطر", 1, 10), ("عيد الأضحى", 10, 12)]:
            for year in (hy, hy + 1):
                stamp = self.json(f"https://api.aladhan.com/v1/hToG/{hd:02d}-{hm:02d}-{year}")[
                    "data"
                ]["gregorian"]["date"]
                at = datetime.strptime(stamp, "%d-%m-%Y").date()
                if at > day.date():
                    events.append((at, name))
                    break
        at, event = min(events)
        weather = self.json(
            "https://api.open-meteo.com/v1/forecast?latitude=24.71&longitude=46.68&timezone=Asia%2FRiyadh&forecast_days=1&daily=temperature_2m_max,temperature_2m_min"
        )
        other = self.json(
            "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=24.71&lon=46.68"
        )
        temperatures = [
            r["data"]["instant"]["details"]["air_temperature"]
            for r in other["properties"]["timeseries"]
            if datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
            .astimezone(ZoneInfo("Asia/Riyadh"))
            .date()
            == day.date()
        ]
        hi, lo = (
            weather["daily"]["temperature_2m_max"][0],
            weather["daily"]["temperature_2m_min"][0],
        )
        if not temperatures or abs(hi - max(temperatures)) > 3 or abs(lo - min(temperatures)) > 3:
            raise ValueError("LEGACY_BRIEF_WEATHER_DISAGREES")
        fact = data["facts"][day.toordinal() % len(data["facts"])]
        hist, year = data["history"][day.toordinal() % len(data["history"])]
        verses = self.quran(94, [5, 6], background=True)
        self.brief_audio = []
        cursor = 3.2
        for v in verses:
            self.brief_audio.append(
                {"file": v["audio"], "at": cursor, "seconds": v["audio_duration"]}
            )
            cursor += v["audio_duration"]
        return [
            scene(prayers, 12.0, role="brief", head="أوقات الصلاة • الرياض"),
            scene(
                f"باقي {(at - day.date()).days} يوم على {event}".translate(DIGITS),
                12.0,
                role="brief",
                head="العدّاد",
            ),
            scene(fact, 12.0, role="brief", head="هل تعلم؟"),
            scene(
                hist,
                9.0,
                role="brief",
                head=f"من تاريخ الإسلام • {year} هـ",
                source=f"الرياض: العظمى {hi} والصغرى {lo} — متحقق من مصدرين",
            ),
        ]


def prepare(app: str, key: str, job: str, root: Path, **kwargs: Any) -> dict[str, Any]:
    if (root / "content.json").is_file():
        value = json.loads((root / "content.json").read_bytes())
        p = validate(value, assets=root)
        if p["app"] != app or p["key"] != key:
            raise ValueError("LEGACY_CACHED_SCOPE_CHANGED")
        return cast(dict[str, Any], value)
    return Factory(app, key, job, root, **kwargs).build()


def safe_xml(raw: bytes) -> ET.Element:
    if len(raw) > 4_000_000 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("LEGACY_XML_UNSAFE")
    return ET.fromstring(raw)  # noqa: S314 — entities/DOCTYPE rejected above
