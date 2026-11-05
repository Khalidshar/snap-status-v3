"""Requested league coverage and case-independent brand priority."""

import pytest

from studio_core.sources.news import matches, top_brand


@pytest.mark.parametrize(
    "text", ["الدوري السعودي", "الدوري المصري", "الدوري المغربي", "الدوري الأوروبي"]
)
def test_football_covers_all_requested_leagues(text):
    assert matches("football", text)


def test_brand_priority_is_case_insensitive():
    assert top_brand("style", "New DIOR perfume")
    assert top_brand("style", "عطر جديد من ديور")
    assert not top_brand("tech", "Dior")
