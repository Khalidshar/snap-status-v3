"""Feed dates: the standard form and the Arabic civil form (repair 11 Nov 2026)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from studio_core.production.parsing import feed_time

CAIRO = ZoneInfo("Africa/Cairo")


def test_standard_feed_date():
    assert feed_time("Tue, 10 Nov 2026 20:55:00 +0300") == 1794333300.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("الأربعاء، 11 نوفمبر 2026 01:00 ص", datetime(2026, 11, 11, 1, 0, tzinfo=CAIRO)),
        ("الثلاثاء، 10 نوفمبر 2026 11:30 م", datetime(2026, 11, 10, 23, 30, tzinfo=CAIRO)),
        ("الأحد، 1 مارس 2026 12:05 ص", datetime(2026, 3, 1, 0, 5, tzinfo=CAIRO)),
        ("الأحد، 1 مارس 2026 12:05 م", datetime(2026, 3, 1, 12, 5, tzinfo=CAIRO)),
    ],
)
def test_arabic_feed_date_is_cairo_time(text, expected):
    assert feed_time(text) == expected.timestamp()


@pytest.mark.parametrize("text", ["", "قبل قليل", "11 شوال 2026 01:00 ص", "yesterday"])
def test_unknown_feed_date_stays_an_error(text):
    with pytest.raises(ValueError):
        feed_time(text)
