"""Arabic, topic-scoped live headlines for review. Never approves or sends content."""

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from studio_core.network.client import NetworkError
from studio_core.sources import Source, command_key, for_command
from studio_core.sources.audit import Client
from studio_core.sources.feeds import _plain, parse_feed

TITLES = {
    "urgent": "خبر عاجل",
    "saudi": "أخبار السعودية",
    "world": "أخبار العالم",
    "news": "موجز الأخبار",
    "football": "كرة القدم",
    "markets": "الأسواق والاقتصاد",
    "tech": "التقنية",
    "style": "الأناقة والجمال",
}
MAX_AGE = {key: 86400 for key in TITLES} | {"urgent": 3600, "tech": 259200, "style": 604800}
WORDS = {
    "urgent": (
        "عاجل",
        "هجوم",
        "قصف",
        "غارة",
        "غارات",
        "صاروخ",
        "صواريخ",
        "انفجار",
        "إطلاق نار",
        "اعتراض",
        "مسيرة",
        "مسيّرة",
        "اشتباك",
        "إنذار",
    ),
    "saudi": (
        "السعود",
        "الرياض",
        "جدة",
        "مكة",
        "المدينة المنورة",
        "ولي العهد السعودي",
        "دوري روشن",
        "نيوم",
        "أرامكو",
    ),
    "world": (
        "أوكران",
        "روسيا",
        "إيران",
        "اليمن",
        "فلسطين",
        "غزة",
        "إسرائيل",
        "لبنان",
        "سوريا",
        "العراق",
        "إثيوبيا",
        "السودان",
        "أمريكا",
        "الأمريك",
        "أوروبا",
        "الصين",
        "فرنسا",
        "بريطانيا",
        "مصر",
        "تركيا",
        "باكستان",
        "الهند",
        "ألمانيا",
        "إيطاليا",
    ),
    "football": (
        "كرة القدم",
        "دوري روشن",
        "الدوري السعودي",
        "الدوري المصري",
        "دوري أبطال",
        "الدوري الأوروبي",
        "الدوري الإنجليزي",
        "الدوري الإسباني",
        "الدوري الإيطالي",
        "الدوري الألماني",
        "الدوري الفرنسي",
        "كأس العالم",
        "كأس أمم",
        "مباراة",
        "مانشستر",
        "ريال مدريد",
        "برشلونة",
        "ليفربول",
        "الأهلي",
        "الزمالك",
        "الهلال",
        "النصر",
        "الاتحاد",
    ),
    "markets": (
        "اقتصاد",
        "الأسهم",
        "البورصة",
        "تداول",
        "أسواق",
        "الأسواق",
        "النفط",
        "التضخم",
        "الفائدة",
        "البنوك",
        "الذهب",
        "الأرباح",
        "الريال",
        "الدولار",
    ),
    "style": (
        "موضة",
        "أزياء",
        "ازياء",
        "مكياج",
        "تجميل",
        "عطور",
        "عطر",
        "ملابس",
        "أحذية",
        "حقائب",
        "ماركات",
        "مجوهرات",
    ),
}
SPORTS_EXCLUDED = ("كرة السلة", "كرة اليد", "كرة الطائرة", "التنس", "الفورمولا", "الجولف")
DAYS = ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد")
MONTHS = (
    "يناير",
    "فبراير",
    "مارس",
    "أبريل",
    "مايو",
    "يونيو",
    "يوليو",
    "أغسطس",
    "سبتمبر",
    "أكتوبر",
    "نوفمبر",
    "ديسمبر",
)
DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


class NewsUnavailable(ValueError):
    pass


def arabic(text: str) -> bool:
    letters = [c for c in text if unicodedata.category(c).startswith("L")]
    count = sum("ARABIC" in unicodedata.name(c, "") for c in letters)
    return bool(letters) and count / len(letters) >= 0.65 and not re.search("[A-Za-z]{4,}", text)


def fingerprint(text: str) -> str:
    from studio_core.commands import normalize

    return hashlib.sha256(normalize(_plain(text)).encode()).hexdigest()


def matches(key: str, text: str) -> bool:
    if key not in TITLES:
        raise ValueError("Unsupported live news command")
    if key == "football" and any(word in text for word in SPORTS_EXCLUDED):
        return False
    if key == "urgent":
        security = (
            "صاروخ",
            "غارة",
            "غارات",
            "قصف",
            "انفجار",
            "مسيّرة",
            "اعتراض",
            "الجيش",
            "عسكري",
            "مسلح",
            "إطلاق نار",
            "عدوان",
            "قوات",
        )
        return "عاجل" in text or (
            any(word in text for word in WORDS[key]) and any(word in text for word in security)
        )
    return key in {"news", "tech"} or any(word in text for word in WORDS[key])


def published_label(timestamp: float) -> str:
    d = datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Riyadh"))
    time = f"{d.hour % 12 or 12}:{d.minute:02d} {'ص' if d.hour < 12 else 'م'}"
    return f"{DAYS[d.weekday()]} - {d.day} - {MONTHS[d.month - 1]} - {d.year} - {time}".translate(
        DIGITS
    )


def _link(value: str, source: Source) -> str:
    parts = urlsplit(value)
    host = parts.hostname or ""
    publisher = urlsplit(source.url).hostname or ""
    permitted = {publisher, publisher.removeprefix("www."), "www." + publisher.removeprefix("www.")}
    if source.id == "bbc-ar":
        permitted |= {"www.bbc.com", "www.bbc.co.uk", "bbc.com", "bbc.co.uk"}
    if (
        parts.scheme != "https"
        or host not in permitted
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
    ):
        raise ValueError("Foreign or unsafe article link")
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query)
        if not k.startswith("utm_") and k not in {"fbclid", "gclid"}
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {k: v for k, v in value.items() if k != "content_hash"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def collect(
    command: str,
    client: Client,
    now: float,
    *,
    seen: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    key = command_key(command)
    if key not in TITLES or type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("Unsupported news request")
    candidates: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for source in for_command(key):
        # Candidates may be fetched for PRIVATE REVIEW, never treated as publication approval.
        if source.kind != "rss" or source.status != "candidate" or source.language != "ar":
            continue
        try:
            response = client.get(source.url)
            if (
                not 200 <= response.status < 300
                or hashlib.sha256(response.body).hexdigest() != response.sha256
            ):
                raise ValueError("Invalid source response")
            items = parse_feed(
                response.body,
                {
                    "name": source.name,
                    "url": source.url,
                    "language": "ar",
                    "section": "news",
                },
                style_only=False,
            )
            for item in items:
                try:
                    title = item["title"]
                    at = datetime.fromisoformat(item["date"]).timestamp()
                    mark = fingerprint(title)
                    if (
                        not 1 <= len(title) <= 180
                        or not arabic(title)
                        or not matches(key, title)
                        or not 0 <= now - at < MAX_AGE[key]
                        or mark in seen
                    ):
                        continue
                    candidates.append(
                        {
                            "headline": title,
                            "source_id": source.id,
                            "source": source.name,
                            "source_url": _link(item["link"], source),
                            "published_at": at,
                            "published_label": published_label(at),
                            "fingerprint": mark,
                        }
                    )
                except (ValueError, OverflowError):
                    continue
        except (NetworkError, ValueError) as error:
            failures.append({"source_id": source.id, "error_type": type(error).__name__})
    if not candidates:
        raise NewsUnavailable("لا يوجد خبر عربي حديث مناسب للأمر؛ لم يُستخدم محتوى ثابت بديل.")
    # No mutation of seen and no write to delivery history: review is not publication.
    selected = max(candidates, key=lambda entry: (entry["published_at"], entry["fingerprint"]))
    content: dict[str, Any] = {
        "schema": 1,
        "type": "news_review",
        "command": key,
        "title": TITLES[key],
        "language": "ar",
        "sacred": False,
        "publication_enabled": False,
        "audio_review": "PENDING_HUMAN_AUDIO",
        **selected,
        "fetched_at": now,
        "expires_at": min(now + 1800, selected["published_at"] + MAX_AGE[key]),
    }
    content["content_hash"] = digest(content)
    return {"content": content, "failures": failures, "external_delivery": False}


def validate(content: dict[str, Any], now: float) -> None:
    """Revalidate frozen bytes offline immediately before rendering; no refetch."""
    if (
        content.get("schema") != 1
        or content.get("type") != "news_review"
        or content.get("command") not in TITLES
        or content.get("language") != "ar"
        or content.get("sacred") is not False
        or content.get("publication_enabled") is not False
        or content.get("audio_review") != "PENDING_HUMAN_AUDIO"
        or content.get("content_hash") != digest(content)
    ):
        raise ValueError("Invalid news review")
    key = content["command"]
    source = next((s for s in for_command(key) if s.id == content.get("source_id")), None)
    if (
        source is None
        or source.kind != "rss"
        or source.status != "candidate"
        or source.language != "ar"
    ):
        raise ValueError("Unapproved review source")
    title = _plain(content["headline"])
    at, fetched, expiry = (content[k] for k in ("published_at", "fetched_at", "expires_at"))
    if (
        any(type(t) not in (int, float) or not math.isfinite(t) for t in (now, at, fetched, expiry))
        or not 0 <= now - at < MAX_AGE[key]
        or not at <= fetched <= now < expiry
        or expiry != min(fetched + 1800, at + MAX_AGE[key])
        or not 1 <= len(title) <= 180
        or not arabic(title)
        or not matches(key, title)
        or content.get("fingerprint") != fingerprint(title)
        or content.get("title") != TITLES[key]
        or content.get("source") != source.name
        or content.get("published_label") != published_label(at)
        or _link(content["source_url"], source) != content["source_url"]
    ):
        raise ValueError("Changed, expired or misclassified news review")
