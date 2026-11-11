"""The clip's date label is the day and date only (owner 12 Nov 2026)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from studio_core.sources.news import published_label

RIYADH = ZoneInfo("Asia/Riyadh")


def test_label_has_day_and_date_without_clock_time():
    late = datetime(2026, 11, 11, 22, 47, tzinfo=RIYADH).timestamp()
    early = datetime(2026, 11, 11, 1, 5, tzinfo=RIYADH).timestamp()
    assert published_label(late) == published_label(early) == "الأربعاء - ١١ - نوفمبر - ٢٠٢٦"


def test_label_follows_the_riyadh_day():
    # 23:30 UTC on the 11th is already Thursday the 12th in Riyadh.
    stamp = datetime(2026, 11, 11, 23, 30, tzinfo=ZoneInfo("UTC")).timestamp()
    assert published_label(stamp) == "الخميس - ١٢ - نوفمبر - ٢٠٢٦"
