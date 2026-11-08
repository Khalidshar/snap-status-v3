# ruff: noqa: E501, E741, RUF001
# mypy: ignore-errors
"""Source-only extraction of the reviewed old parsers; no transport or execution.

See config/production/legacy-sources.json for pinned input blobs.
"""

import datetime as dt
import html
import re

TZ = dt.timezone(dt.timedelta(hours=3))


def clean(text: str, limit: int = 0) -> str:
    t = html.unescape(re.sub("<[^>]+>", " ", text or ""))
    # Source emoji are decoration and the approved emoji font lacks many of them (8 Nov 2026:
    # «📷» failed every tech clip on the glyph gate). Every letter and digit stays.
    t = re.sub("[\U0001f000-\U0001faff☀-➿️‍]", "", t)
    t = re.sub("\\s+", " ", t).strip()
    t = re.sub("(ظهرت المقالة|The post) .*$", "", t).strip()
    if limit and len(t) > limit:
        cut = t[:limit]
        m = max(cut.rfind("."), cut.rfind("،"), cut.rfind("؛"))
        t = (cut[:m] if m > limit * 0.5 else cut.rsplit(" ", 1)[0]) + "…"
    return t


BOILER = (
    "اقرأ أيضا",
    "اقرأ أيضًا",
    "اقرئي أيضا",
    "اقرئي أيضًا",
    "شاهد أيضا",
    "تابعونا",
    "اشترك",
    "حقوق",
    "©",
    "إعلان",
    "The post",
    "ظهرت المقالة",
    "لمزيد من",
    "تابعوا",
    "Cookie",
    "cookies",
    "للاطلاع على",
)


def img_src(tag: str) -> str:
    for attr in ("data-src", "data-lazy-src", "data-original", "src", "srcset", "data-srcset"):
        m = re.search(attr + "\\s*=\\s*[\\\"']([^\\\"'\\s,]+)", tag)
        if m and (
            not re.search(
                "\\.(svg|gif)|data:|logo|icon|avatar|pixel|1x1|placeholder", m.group(1), re.I
            )
        ):
            return html.unescape(m.group(1))
    return ""


def body_text(page: str) -> str:
    """Readable article text (headings marked with ##, pictures as [[IMG]] url) from an article page or feed content."""
    page = re.sub(
        "(?is)<(script|style|nav|header|footer|aside|form|noscript|svg|button)[^>]*>.*?</\\1>",
        " ",
        page,
    )
    m = re.search("(?is)<article[^>]*>(.*?)</article>", page)
    body = m.group(1) if m else page
    lines: list[str] = []
    for tag, t, img in re.findall("(?is)<(h2|h3|h4|p|li)[^>]*>(.*?)</\\1>|(<img[^>]+>)", body):
        if img:
            u = img_src(img)
            if u and len([x for x in lines if x.startswith("[[IMG]]")]) < 12:
                lines.append("[[IMG]] " + u)
            continue
        t = clean(t)
        if not t or any(w in t for w in BOILER) or (tag in ("p", "li") and len(t) < 30):
            continue
        t = ("## " if tag.lower().startswith("h") else "") + t
        if t not in lines:
            lines.append(t)
    return "\n".join(lines)


def words_only(text: str) -> str:
    return "\n".join(x for x in text.split("\n") if not x.startswith("[[IMG]]"))


def plain_points(text: str, desc: str) -> list[dict]:
    """Without the AI: headings with their first paragraph, else the first paragraphs, trimmed."""
    lines = [l for l in text.split("\n") if l.strip()]
    pts: list[dict] = []
    for n, l in enumerate(lines):
        if l.startswith("## "):
            after = lines[n + 1 : n + 5]
            nxt = next((x for x in after if not x.startswith(("## ", "[[IMG]]"))), "")
            pic = next((x[8:].strip() for x in after[:3] if x.startswith("[[IMG]]")), "")
            pts.append(
                {
                    "head": re.sub("^\\s*[\\d٠-٩]+\\s*[-–.)]\\s*", "", l[3:])[:70],
                    "body": clean(nxt, 170),
                    "pic": pic,
                }
            )
    lines = [l for l in lines if not l.startswith("[[IMG]]")]
    if len(pts) < 2:
        pts = [{"head": "", "body": clean(l.lstrip("# "), 190)} for l in lines if len(l) > 40][:5]
    return pts[:6] or [{"head": "", "body": clean(desc, 200)}]


CONFLICTS = {
    "روسيا وأوكرانيا": ("روسيا", "أوكرانيا", "كييف", "موسكو", "دونباس", "القرم"),
    "إسرائيل وفلسطين": ("فلسطين", "غزة", "الضفة", "إسرائيل", "إسرائيلي", "حماس", "رفح", "الأقصى"),
    "إسرائيل ولبنان": ("لبنان", "حزب الله", "الجنوب اللبناني", "إسرائيل", "إسرائيلي"),
    "إيران وإسرائيل": ("إيران", "إيراني", "إسرائيل", "إسرائيلي", "طهران", "تل أبيب"),
    "اليمن والبحر الأحمر": ("اليمن", "الحوثي", "البحر الأحمر", "باب المندب", "المخا", "مأرب"),
    "السودان": ("السودان", "الخرطوم", "الدعم السريع", "الجيش السوداني", "دارفور"),
    "سوريا": ("سوريا", "سوري", "دمشق", "حلب", "إدلب", "قسد"),
    "العراق": ("العراق", "عراقي", "بغداد", "أربيل", "الحشد"),
    "ليبيا": ("ليبيا", "ليبي", "طرابلس", "بنغازي"),
    "إثيوبيا": ("إثيوبيا", "تيغراي", "أمهرة", "أوروميا"),
    "الصومال": ("الصومال", "صومالي", "مقديشو", "حركة الشباب"),
    "السعودية واليمن": ("السعودية", "السعودي", "اليمن", "الحوثي", "الحدود السعودية"),
    "تركيا وسوريا": ("تركيا", "تركي", "سوريا", "سوري", "أنقرة", "قسد"),
    "تركيا والعراق": ("تركيا", "تركي", "العراق", "عراقي", "حزب العمال"),
    "أذربيجان وأرمينيا": ("أذربيجان", "أرمينيا", "قره باغ", "كاراباخ"),
    "الهند وباكستان": ("الهند", "هندي", "باكستان", "باكستاني", "كشمير"),
    "الكونغو الديمقراطية": ("الكونغو الديمقراطية", "غوما", "حركة إم23", "إم23"),
    "الساحل الأفريقي": ("مالي", "بوركينا فاسو", "النيجر", "الساحل", "جماعة نصرة الإسلام"),
    "ميانمار": ("ميانمار", "بورما", "أراكان"),
    "أفغانستان": ("أفغانستان", "طالبان", "كابل"),
    "الكوريتان": ("كوريا الشمالية", "كوريا الجنوبية", "بيونغ يانغ", "سيول"),
    "الصين وتايوان": ("الصين", "تايوان", "بكين", "تايبيه", "مضيق تايوان"),
    "بحر الصين الجنوبي": ("بحر الصين الجنوبي", "الفلبين", "الصين", "فيتنام", "جزر سبراتلي"),
}

URGENT_WORDS = (
    "عاجل",
    "الآن",
    "قبل قليل",
    "هجوم",
    "غارات",
    "قصف",
    "صاروخ",
    "صواريخ",
    "مسيرة",
    "مسيّرة",
    "اشتباكات",
    "استهداف",
    "انفجار",
    "إطلاق نار",
    "تحذير",
    "إنذار",
    "اعتراض",
    "توغل",
)

STATEMENT_WORDS = (
    "أعلن",
    "أعلنت",
    "يعلن",
    "يؤكد",
    "تؤكد",
    "أكد",
    "أكدت",
    "نفى",
    "نفت",
    "قال",
    "قالت",
    "بيان",
    "تصريح",
    "تصريحات",
)


def classify_conflict(text: str) -> str:
    t = text.lower()
    for name, keys in CONFLICTS.items():
        if any(k.lower() in t for k in keys):
            return name
    return ""


def is_urgent(item: dict) -> bool:
    text = f"{item.get('title', '')} {item.get('detail', '')}".lower()
    return bool(
        (item.get("conflict") or classify_conflict(text))
        and (
            any(k.lower() in text for k in URGENT_WORDS)
            or any(k.lower() in text for k in STATEMENT_WORDS)
        )
    )


def urgency_score(item: dict) -> int:
    text = f"{item.get('title', '')} {item.get('detail', '')}".lower()
    score = 100 if any(k in text for k in ("عاجل", "الآن", "قبل قليل")) else 0
    score += 35 * sum(k.lower() in text for k in URGENT_WORDS)
    score += 12 * sum(k.lower() in text for k in STATEMENT_WORDS)
    age = max(
        0, (dt.datetime.now(TZ) - item.get("published", dt.datetime.now(TZ))).total_seconds() / 3600
    )
    return score + max(0, int(24 - age))


def conflict_label(item: dict) -> str:
    return (
        item.get("conflict")
        or classify_conflict(f"{item.get('title', '')} {item.get('detail', '')}")
        or "دولي"
    )


def words(t: str) -> set:
    return {
        w
        for w in re.findall("[\\w\u0600-ۿ]{3,}", t)
        if w not in ("على", "إلى", "التي", "الذي", "بعد", "قبل", "حول")
    }


def same_story(title: str, titles: list[str]) -> bool:
    a = words(title)
    return any(len(a & (b := words(x))) >= max(3, 0.5 * min(len(a), len(b) or 1)) for x in titles)


def _img(it) -> str:
    for el in it.iter():
        tag = el.tag.split("}")[-1]
        u = el.get("url") or ""
        if (
            tag in ("content", "thumbnail", "enclosure")
            and u
            and (not re.search("\\.(mp4|mp3|m3u8)", u))
        ):
            return u
    m = re.search("<img[^>]+src=[\\\"']([^\\\"']+)", it.findtext("description") or "")
    return html.unescape(m.group(1)) if m else ""
