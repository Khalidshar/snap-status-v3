import pytest

from studio_core.sources.news import matches


@pytest.mark.parametrize(
    "headline",
    [
        "نتائج الدوري المغربي اليوم",
        "الوداد المغربي يتصدر الترتيب",
        "الرجاء المغربي يعلن تشكيلته",
        "نهضة بركان يستعد للجولة",
        "أخبار الجيش الملكي في البطولة الاحترافية المغربية",
    ],
)
def test_morocco_league_is_football(headline):
    assert matches("football", headline)


def test_morocco_other_sport_is_not_football():
    assert not matches("football", "الدوري المغربي لكرة السلة")
