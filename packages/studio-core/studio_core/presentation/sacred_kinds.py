"""Verbatim Quran, mixed adhkar, hadith, dua and fasting content kinds.

The text is the exact faith.py text, sha256-checked on every load. Nothing rewrites,
proofreads or strips it; long Quran/adhkar bodies are losslessly paginated by Pango.
"""

from datetime import date
from typing import Any

from studio_core.content import ContentPackage, text_hash

from . import faith
from .did_you_know import register_kind

TITLES = {
    "hadith": "حديث شريف",
    "dua": "دعاء",
    "fast": "تذكير الصيام",
    "verse": "آية اليوم",
    "adhkar_am": "أذكار الصباح",
    "adhkar_pm": "أذكار المساء",
    "friday": "رسالة الجمعة",
    "kahf": "سورة الكهف",
}
SURAH_NAMES = {
    2: "البقرة",
    3: "آل عمران",
    11: "هود",
    13: "الرعد",
    14: "إبراهيم",
    15: "الحجر",
    16: "النحل",
    17: "الإسراء",
    18: "الكهف",
    20: "طه",
    21: "الأنبياء",
    24: "النور",
    25: "الفرقان",
    29: "العنكبوت",
    30: "الروم",
    33: "الأحزاب",
    36: "يس",
    39: "الزمر",
    40: "غافر",
    42: "الشورى",
    50: "ق",
    55: "الرحمن",
    62: "الجمعة",
    65: "الطلاق",
    67: "الملك",
    89: "الفجر",
    93: "الضحى",
    94: "الشرح",
    103: "العصر",
    108: "الكوثر",
    112: "الإخلاص",
}


def reference(item: dict[str, Any], *, compact: bool = False) -> str:
    """Display metadata only; never included in or substituted for the locked text."""
    digits = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
    ayahs = "، ".join(str(a).translate(digits) for a in item["ayahs"])
    if (
        compact
        and len(item["ayahs"]) > 1
        and item["ayahs"] == list(range(item["ayahs"][0], item["ayahs"][-1] + 1))
    ):
        ayahs = (str(item["ayahs"][0]) + "\u2013" + str(item["ayahs"][-1])).translate(digits)
    return (
        "سورة "
        + SURAH_NAMES[item["surah"]]
        + " • "
        + ("الآية " if len(item["ayahs"]) == 1 else "الآيات ")
        + ayahs
    )


# The owner approved these catalogs, with the 2026-09-30 Quran package, on 2026-10-28.
REVIEWED = "2026-10-28"


def loader(kind: str) -> Any:
    def load(today: date) -> list[dict[str, Any]]:
        del today  # faith.py checks its own catalog
        return [
            {
                "id": item["id"],
                "category": kind,
                "text": faith.text(item),
                "source": (
                    reference(item, compact=kind == "kahf")
                    if kind in {"verse", "friday", "kahf"}
                    else item["source"]
                ),
                "source_url": item.get("verification_url", ""),
                "verified_at": REVIEWED,
            }
            for item in faith.load(kind)
        ]

    return load


def verbatim(kind: str, fact_id: object, body: object) -> str:
    """The sha256 of the approved text, or ValueError when the body is not exactly it."""
    item = next((i for i in faith.load(kind) if i["id"] == fact_id), None)
    if item is None or not isinstance(body, str):
        raise ValueError("SACRED_TEXT_CHANGED")
    reference_hash = text_hash(faith.text(item))
    package = ContentPackage(
        id=item["id"],
        type=kind,
        title=TITLES[kind],
        body=body,
        language="ar",
        source=item.get("source", ""),
        source_url=item.get("verification_url", ""),
        destination="",
        sacred=True,
    )
    try:
        package.sacred_lock(reference_hash)
    except ValueError as error:
        raise ValueError("SACRED_TEXT_CHANGED") from error
    return reference_hash


for _kind, _title in TITLES.items():
    register_kind(_kind, _title, loader(_kind), sacred=True)
