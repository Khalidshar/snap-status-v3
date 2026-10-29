"""Hadith, dua and fasting reminders as sacred content kinds (commands 15, 16 and 20).

The text is the exact faith.py text, sha256-checked on every load. Nothing rewrites,
proofreads, wraps or strips it; the renderer draws it whole with HarfBuzz shaping.
"""

from datetime import date
from typing import Any

from . import faith
from .did_you_know import register_kind

TITLES = {
    "hadith": "حديث شريف",
    "dua": "دعاء",
    "fast": "تذكير الصيام",
}
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
                "source": item["source"],
                "source_url": item.get("verification_url", ""),
                "verified_at": REVIEWED,
            }
            for item in faith.load(kind)
        ]

    return load


def verbatim(kind: str, fact_id: object, body: object) -> str:
    """The sha256 of the approved text, or ValueError when the body is not exactly it."""
    item = next((i for i in faith.load(kind) if i["id"] == fact_id), None)
    if item is None or body != faith.text(item):
        raise ValueError("SACRED_TEXT_CHANGED")
    return str(item["sha256"])


for _kind, _title in TITLES.items():
    register_kind(_kind, _title, loader(_kind), sacred=True)
