"""Offline Umm al-Qura lookup derived from hijridate 2.5.0 (MIT).

See UMMALQURA-LICENSE.txt. The pinned table is bundled with every release;
no approximate calendar or network fallback is allowed.
"""

import hashlib
import json
from bisect import bisect_right
from datetime import date
from functools import lru_cache
from pathlib import Path

TABLE_SHA256 = "9eaec9148a72fe105deb39a6ba633aed865dc41f260ce3aef4b23730e71e6b37"


@lru_cache(maxsize=1)
def table() -> tuple[int, int, int, tuple[int, ...]]:
    try:
        raw = Path(__file__).with_name("ummalqura-month-starts.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != TABLE_SHA256:
            raise ValueError("UMM_AL_QURA_TABLE_INVALID")
        data = json.loads(raw)
        lower, upper = (date(*value).toordinal() for value in data["gregorian_range"])
        return lower, upper, int(data["hijri_offset"]), tuple(data["month_starts"])
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError("UMM_AL_QURA_TABLE_INVALID") from error


def convert(day: date) -> tuple[int, int, int]:
    if type(day) is not date:
        raise ValueError("HADITH_GREGORIAN_DAY_REQUIRED")
    lower, upper, offset, starts = table()
    ordinal = day.toordinal()
    if not lower <= ordinal <= upper:
        raise ValueError("UMM_AL_QURA_DATE_UNAVAILABLE")
    # Same Julian/reduced-Julian offset as hijridate 2.5.0 helpers.py.
    reduced = ordinal + 1721425 - 2400000
    index = bisect_right(starts, reduced) - 1
    year, month = divmod(index + offset, 12)
    return year + 1, month + 1, reduced - starts[index] + 1
