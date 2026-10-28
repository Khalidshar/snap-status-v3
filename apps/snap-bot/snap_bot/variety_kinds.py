"""The reviewed variety catalogs as content kinds of the fact pipeline (commands 8, 9, 11, 12).

Each item becomes a fact shaped like the did_you_know catalog, with its kind as its only
category. Titles are plain Arabic: the card font has no emoji glyphs. The riddle catalog
waits for its own two-part card.
"""

from datetime import date
from typing import Any

from . import catalogs
from .did_you_know import register_kind

TITLES = {
    "tech": "تقنية",
    "style": "أناقة وجمال",
    "funny": "مواقف طريفة",
    "night": "قبل النوم",
}
# Short display names keep the attribution on one line of the card, like NASA or UNESCO.
SHORT_SOURCES = {
    "المركز الوطني البريطاني للأمن السيبراني": "NCSC",
    "وكالة الأمن السيبراني الأمريكية": "CISA",
    "دعم أبل": "Apple",
    "دعم جوجل": "Google",
    "دعم مايكروسوفت": "Microsoft",
    "جامعة كارولاينا الشمالية في شارلوت": "جامعة شارلوت",
    "الأكاديمية الأمريكية للأمراض الجلدية": "AAD",
}


def loader(kind: str) -> Any:
    def load(today: date) -> list[dict[str, Any]]:
        del today  # the catalog checks its own verification dates
        return [
            {
                "id": item["id"],
                "category": kind,
                "text": catalogs.body(item, kind),
                "source": SHORT_SOURCES.get(item["source"], item["source"]),
                "source_url": item.get("source_url", ""),
                "verified_at": item["verified_at"],
            }
            for item in catalogs.load(kind)
        ]

    return load


for _kind, _title in TITLES.items():
    register_kind(_kind, _title, loader(_kind))
