"""Owner-approved fashion desks and image review evidence; no network or scheduling."""

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

TITLES = {
    "style": "جديد البراندات",
    "style_women": "أناقة المرأة",
    "style_men": "أناقة الرجل",
    "style_perfume": "عالم العطور",
    "style_couples": "للأزواج",
    "style_kids": "أناقة الأطفال",
}
KEYS = frozenset(TITLES)
# Named houses only from DECISIONS (10 November); no inferred additions.
BRANDS = (
    ("ديور", "dior"), ("شانيل", "chanel"), ("برادا", "prada"),
    ("كالفن كلاين", "calvin klein"), ("مايكل كورس", "michael kors"),
    ("أرماني", "ارماني", "armani"), ("لويس فيتون", "لويس فويتون", "louis vuitton"),
    ("غوتشي", "غوتشى", "gucci"), ("هيرميس", "hermes", "hermès"),
    ("فيرساتشي", "versace"), ("فالنتينو", "valentino"),
    ("بالنسياغا", "balenciaga"), ("سان لوران", "saint laurent"),
    ("جيفنشي", "givenchy"), ("دولتشي آند غابانا", "دولتشي اند غابانا", "dolce & gabbana"),
    ("بربري", "burberry"), ("فندي", "fendi"), ("كوتش", "coach"),
    ("تومي هيلفيغر", "tommy hilfiger"), ("هوغو بوس", "hugo boss"),
    ("رالف لورين", "ralph lauren"), ("كيت سبيد", "kate spade"),
    ("توري بيرش", "tory burch"), ("لاكوست", "lacoste"),
    ("ماسيمو دوتي", "massimo dutti"),
)  # fmt: skip
LOCAL = (
    "العربية للعود", "القرشي", "ابراق", "إبراق", "دار الاميرات", "دار الأميرات",
    "اجمل", "أجمل", "الرصاصي", "لطافة", "لطافه", "هدى بيوتي", "huda beauty",
    "arabian oud", "al qurashi", "ajmal", "rasasi", "lattafa", "ibrahim alqurashi",
    "ماركة محلية", "علامة محلية", "علامة سعودية", "علامة خليجية", "local brand",
)  # fmt: skip
SECTIONS = (
    ("style_kids", r"اطفال|للطفل|للصغار|المدارس|\b(?:kids|children|childrenswear|baby|junior)\b"),
    ("style_couples", r"ازواج|للزوجين|للزوج|للزوجه|زفاف|\b(?:couples|wedding|his and hers)\b"),
    ("style_perfume", r"عطر|عطور|بخور|دهن العود|\b(?:perfume|perfumes|fragrance|fragrances|oud|cologne)\b"),
    ("style_men", r"رجالي|للرجل|الرجل|للرجال|\b(?:menswear|men|mens|homme)\b"),
    ("style_women", r"نسائي|للمراه|للمرأه|نساء|مكياج|بشره|حقائب|مجوهرات|فساتين|\b(?:women|womenswear|makeup|skincare|handbags|jewelry)\b"),
)  # fmt: skip


def normalize(text: str) -> str:
    return re.sub(r"[\u064b-\u065f\u0670\u0640]", "", text.casefold()).translate(
        str.maketrans("أإآٱةى", "ااااهى")
    )


def approved_brand(text: str) -> bool:
    value = normalize(text)
    if any(normalize(word) in value for word in LOCAL):
        return False
    return any(
        re.search(r"(?<![a-zء-ي])[وفبكل]?(?:لل|ال)?" + re.escape(normalize(alias)) + r"(?![a-zء-ي])", value)
        for aliases in BRANDS for alias in aliases
    )


def section(title: str, detail: str = "") -> str | None:
    value = normalize(title + " " + detail)
    if not approved_brand(value):
        return None
    # Title decides first; detail only fills in a non-specific headline.
    for part in (normalize(title), value):
        for key, pattern in SECTIONS:
            if re.search(pattern, part):
                return key
    return "style"


def safe_url(value: str) -> bool:
    try:
        p = urlsplit(value)
        return bool(
            p.scheme == "https" and p.hostname and not p.username and not p.password
            and p.port in (None, 443) and not any(c.isspace() for c in value)
        )
    except ValueError:
        return False


def review_matches(review: Any, image_sha: str, image_url: str, article_url: str) -> bool:
    return bool(
        isinstance(review, dict)
        and review.get("schema") == 1
        and review.get("decision") == "APPROVED"
        and review.get("family_safe") is True
        and review.get("owner_reviewed") is True
        and re.fullmatch("[a-f0-9]{64}", image_sha)
        and review.get("image_sha256") == image_sha
        and review.get("image_url") == image_url
        and review.get("article_url") == article_url
        and safe_url(image_url) and safe_url(article_url)
    )


def image_review(ledger: Path, image_sha: str, image_url: str, article_url: str) -> dict[str, Any] | None:
    if not ledger.is_file():
        return None
    if any(p.is_symlink() for p in (ledger, *ledger.absolute().parents)):
        raise ValueError("FASHION_REVIEW_UNSAFE")
    with ledger.open("rb") as stream:
        raw = stream.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("FASHION_REVIEW_TOO_LARGE")
    found = None
    for line in raw.splitlines():
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError("FASHION_REVIEW_INVALID")
        if (
            record.get("image_sha256") == image_sha and record.get("image_url") == image_url
            and record.get("article_url") == article_url
        ):
            found = record if review_matches(record, image_sha, image_url, article_url) else None
    return found


def record_review(
    ledger: Path, image: Path, image_url: str, article_url: str, *, approve: bool, apply: bool = False
) -> dict[str, Any]:
    if any(p.is_symlink() for p in (image, *image.absolute().parents, ledger, *ledger.absolute().parents)):
        raise ValueError("FASHION_REVIEW_UNSAFE")
    if not safe_url(image_url) or not safe_url(article_url):
        raise ValueError("FASHION_REVIEW_URL_INVALID")
    record = {
        "schema": 1, "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "image_url": image_url, "article_url": article_url,
        "decision": "APPROVED" if approve else "REJECTED",
        "family_safe": approve, "owner_reviewed": True,
    }
    if apply:
        ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(ledger, flags, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write((json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
    return record
