"""Pinned MIT-licensed Umm al-Qura month starts; no external runtime dependency."""

import hashlib
import json
from bisect import bisect_right
from datetime import date
from pathlib import Path

DATA = Path(__file__).with_name("data") / "ummalqura-2.5.0.json"
SHA256 = "bb736aa487b60667745a48242778799facc221ba5483d64c14549d91c710a32b"
MONTHS = (
    "محرم",
    "صفر",
    "ربيع الأول",
    "ربيع الآخر",
    "جمادى الأولى",
    "جمادى الآخرة",
    "رجب",
    "شعبان",
    "رمضان",
    "شوال",
    "ذو القعدة",
    "ذو الحجة",
)


def hijri(day: date) -> tuple[int, int, int]:
    raw = DATA.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError("Umm al-Qura table changed")
    table = json.loads(raw)
    start, end = (date(*parts) for parts in table["range"])
    if not start <= day <= end:
        raise OverflowError("Date outside approved calendar range")
    reduced = day.toordinal() - 678575
    index = bisect_right(table["month_starts"], reduced) - 1
    years, month = divmod(index + table["offset"], 12)
    return years + 1, month + 1, reduced - table["month_starts"][index] + 1
