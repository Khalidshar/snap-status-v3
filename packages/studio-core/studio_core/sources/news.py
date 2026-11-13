"""Arabic, topic-scoped live headlines for review. Never approves or sends content."""

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Callable
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
        "الدوري المغربي",
        "البطولة المغربية",
        "البطولة الاحترافية المغربية",
        "الرجاء المغربي",
        "الوداد المغربي",
        "الجيش الملكي",
        "نهضة بركان",
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
        # Brands, shows, models and celebrity looks (owner request 2026-11-01).
        "شانيل",
        "ديور",
        "غوتشي",
        "لويس فويتون",
        "برادا",
        "هيرميس",
        "فيرساتشي",
        "بربري",
        "فالنتينو",
        "إيلي صعب",
        "زهير مراد",
        "كارتييه",
        "بولغري",
        "لانكوم",
        "إستي لودر",
        "عطر جديد",
        "مجموعة جديدة",
        "أسبوع الموضة",
        "عرض أزياء",
        "عروض الأزياء",
        "عارضة",
        "السجادة الحمراء",
        "إطلالة",
    ),
}
# Other sports, without the article: «التنس», «للتنس» and «بالتنس» are all tennis, while
# «تنسيق» is not (owner repair 11 Nov 2026: the tennis federation entered the football clip).
SPORTS_EXCLUDED = (
    "كرة السلة",
    "كرة اليد",
    "كرة الطائرة",
    "كرة المضرب",
    "تنس",
    "فورمولا",
    "جولف",
    "غولف",
    "ملاكمة",
    "مصارعة",
    "ألعاب القوى",
    "سباحة",
    "دراجات",
    "فروسية",
    "بادل",
    "اسكواش",
    "كريكت",
    "رغبي",
    "سلة",
)
OTHER_SPORT = re.compile(
    "(?<![ء-ي])[وفبك]?(?:ال|لل)?(?:" + "|".join(SPORTS_EXCLUDED) + ")(?![ء-ي])"
)
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


AUDIO_POLICY = "ORIGINAL_SOURCE_AUDIO_OR_SILENT"
MEDIA_POLICY = "SOURCE_VIDEO_ELSE_SOURCE_TEXT"


class NewsUnavailable(ValueError):
    pass


def arabic(text: str) -> bool:
    letters = [c for c in text if unicodedata.category(c).startswith("L")]
    count = sum("ARABIC" in unicodedata.name(c, "") for c in letters)
    return bool(letters) and count / len(letters) >= 0.65 and not re.search("[A-Za-z]{4,}", text)


def fingerprint(text: str) -> str:
    from studio_core.commands import normalize

    return hashlib.sha256(normalize(_plain(text)).encode()).hexdigest()


STYLE_EXCLUDED = (
    "رونالدو",
    "ميسي",
    "الدوري",
    "مباراة",
    "اللحوم",
    "غذائية",
    "سيارة",
    "سيارات",
    # Same names, other businesses: the EFG Hermes bank, brokerage and property finance.
    "إي اف چي",
    "إي إف چي",
    "إي إف جي",
    "اي اف جي",
    "السمسرة",
    "تمويل",
    "العقارية",
    "البورصة",
    "أسهم",
    "أسعار الذهب",
    "شراء الذهب",
)
# Owner request 2026-11-01: the ten strongest houses come first, never as a limit.
TOP_BRANDS = (
    "لويس فويتون",
    "شانيل",
    "هيرميس",
    "غوتشي",
    "ديور",
    "كارتييه",
    "رولكس",
    "برادا",
    "تيفاني",
    "سان لوران",
    "Louis Vuitton",
    "Chanel",
    "Hermès",
    "Gucci",
    "Dior",
    "Cartier",
    "Rolex",
    "Prada",
    "Tiffany",
    "Saint Laurent",
)


def top_brand(key: str, title: str) -> bool:
    return key == "style" and any(brand.casefold() in title.casefold() for brand in TOP_BRANDS)


def football_story(title: str, detail: str = "") -> bool:
    """A political headline needs its own football subject, not a passing body mention."""
    text = title + " " + detail
    if OTHER_SPORT.search(text) or not any(word in text for word in WORDS["football"]):
        return False
    political = (
        "صحافي",
        "صحفي",
        "صحافة",
        "سجن",
        "اعتقال",
        "معتقل",
        "محاكمة",
        "رئيس وزراء",
        "رئيس الوزراء",
        "رئيس الحكومة",
        "سياسي",
        "سياسية",
        "دبلوماسي",
        "دبلوماسية",
        "سفير",
        "سيادة",
        "حدود",
        "انتخابات",
        "صراع",
        "نزاع",
        "أزمة",
        "ازمة",
        "عقوبات",
    )
    if not any(word in title for word in political):
        return True
    # Keep genuine match, player, competition and hosting news even when an official
    # appears in it. The football evidence must be in the headline itself.
    sporting_subject = (
        "كرة القدم",
        "مباراة",
        "مباريات",
        "دوري",
        "الدوري",
        "منتخب",
        "لاعب",
        "مدرب",
        "مهاجم",
        "حارس",
        "هدف",
        "يفوز",
        "فوز",
        "هزيمة",
        "خسارة",
        "تأهل",
        "تتويج",
        "قرعة",
        "استضافة",
        "تنظيم",
        "افتتاح",
        "نهائي",
    )
    return any(word in title for word in WORDS["football"]) and any(
        word in title for word in sporting_subject
    )


_WORLD_AMBIGUOUS_SPORTS = {"دراجات", "سلة", "سباحة", "فروسية"}
_WORLD_SPORTS = re.compile(
    r"(?<![ء-ي])[وفبك]?(?:ال|لل)?(?:"
    + "|".join(
        re.escape(word.translate(str.maketrans("أإآٱ", "اااا")))
        for word in SPORTS_EXCLUDED
        if word not in _WORLD_AMBIGUOUS_SPORTS
    )
    + r"|كرة القدم|مباراة|مباريات|منتخب(?:ات)?|مونديال|"
    r"كاس (?:العالم|امم|اسيا|افريقيا|العرب|الملك|السوبر)|"
    r"دوري (?:ابطال|الابطال|الانجليزي|الاسباني|الايطالي|الالماني|"
    r"الفرنسي|الاوروبي|السعودي|المصري|المغربي)|"
    r"بطولة العالم|العاب (?:ال)?اولمبي[ةه])"
    r"(?![ء-ي])"
)
_WORLD_COMPETITION = re.compile(
    r"(?<![ء-ي])[وفبك]?(?:ال|لل)?(?:بطولة|بطولات|سباق|سباقات|منافسات|ميدالية|ميداليات|ذهبية|فضية|برونزية)(?![ء-ي])"
)


def world_story(title: str, detail: str = "") -> bool:
    # Analysis only: original source strings are never rewritten.
    normalized = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", title).translate(
        str.maketrans("أإآٱ", "اااا")
    )
    normalized = re.sub(r"\b(الرئيس|المجلس|البرلمان|النائب|الحزب) المنتخب\b", r"\1", normalized)
    if _WORLD_SPORTS.search(normalized) or (
        _WORLD_COMPETITION.search(normalized)
        and any(
            re.search(r"(?<![ء-ي])[وفبك]?(?:ال|لل)?" + re.escape(word) + r"(?![ء-ي])", normalized)
            for word in _WORLD_AMBIGUOUS_SPORTS
        )
    ):
        return False
    return any(word in title + " " + detail for word in WORDS["world"])


def matches(key: str, text: str) -> bool:
    if key not in TITLES:
        raise ValueError("Unsupported live news command")
    if key == "world":
        return world_story(text)
    if key == "markets":
        from studio_core.production.parsing import market_story

        return market_story(text)
    if key == "football":
        return football_story(text)
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
    if key == "style" and any(word in text for word in STYLE_EXCLUDED):
        return False
    return key in {"news", "tech"} or any(word in text for word in WORDS[key])


def news_sources(command: str, *, production: bool = False) -> tuple[Source, ...]:
    """Select direct publisher sources for one command before any network request."""
    key = command_key(command)
    return tuple(
        source
        for source in for_command(key)
        if source.status == "approved"
        and urlsplit(source.url).hostname != "news.google.com"
        and (
            (
                source.kind == "rss"
                and (
                    source.language == "ar"
                    or (production and key in {"tech", "style"} and source.language == "en")
                )
            )
            or (
                production
                and key == "style"
                and source.kind == "website"
                and source.language == "ar"
            )
        )
    )


def published_label(timestamp: float) -> str:
    """Day and date only, in Riyadh time (owner 12 Nov 2026: no clock time on any clip)."""
    d = datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Riyadh"))
    return f"{DAYS[d.weekday()]} - {d.day} - {MONTHS[d.month - 1]} - {d.year}".translate(DIGITS)


def _link(value: str, source: Source) -> str:
    parts = urlsplit(value)
    host = parts.hostname or ""
    publisher = urlsplit(source.url).hostname or ""
    permitted = {publisher, publisher.removeprefix("www."), "www." + publisher.removeprefix("www.")}
    if source.id.startswith("bbc-ar"):  # the section feeds too (repair 11 Nov 2026)
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
    source_ids: frozenset[str] | None = None,
    predicate: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    key = command_key(command)
    if key not in TITLES or type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("Unsupported news request")
    candidates: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for source in news_sources(key):
        if source_ids is not None and source.id not in source_ids:
            continue
        try:
            response = client.get(source.url)
            if (
                not 200 <= response.status < 300
                or hashlib.sha256(response.body).hexdigest() != response.sha256
            ):
                raise ValueError("Invalid source response")
            from studio_core.sources.publisher_feeds import normalize

            raw = normalize(source.id, response.body, client)
            items = parse_feed(
                raw,
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
                        or (predicate is not None and not predicate(title))
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
                            **(
                                {"source_video_url": item["video_url"]}
                                if item.get("video_url")
                                else {}
                            ),
                        }
                    )
                except (ValueError, OverflowError):
                    continue
        except (NetworkError, ValueError) as error:
            failures.append({"source_id": source.id, "error_type": type(error).__name__})
    if not candidates:
        raise NewsUnavailable("لا يوجد خبر عربي حديث مناسب للأمر؛ لم يُستخدم محتوى ثابت بديل.")
    # No mutation of seen and no write to delivery history: review is not publication.
    selected = max(
        candidates,
        key=lambda entry: (
            top_brand(key, entry["headline"]),
            entry["published_at"],
            entry["fingerprint"],
        ),
    )
    content: dict[str, Any] = {
        "schema": 1,
        "type": "news_review",
        "command": key,
        "title": TITLES[key],
        "language": "ar",
        "sacred": False,
        "publication_enabled": False,
        "audio_review": AUDIO_POLICY,
        "media_policy": MEDIA_POLICY,
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
        or content.get("audio_review") != AUDIO_POLICY
        or content.get("media_policy") != MEDIA_POLICY
        or content.get("content_hash") != digest(content)
    ):
        raise ValueError("Invalid news review")
    key = content["command"]
    source = next((s for s in news_sources(key) if s.id == content.get("source_id")), None)
    if source is None:
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
