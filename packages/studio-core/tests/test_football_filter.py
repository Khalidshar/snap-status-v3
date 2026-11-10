"""«كرة القدم» is football only: other sports are excluded in any written form (11 Nov 2026)."""

import pytest

from studio_core.sources.news import matches


@pytest.mark.parametrize(
    "text",
    [
        "الجوهرة بنت سعد بن فيصل تدخل سباق رئاسة الاتحاد السعودي للتنس",
        "الهلال يتوج ببطولة كرة السلة بعد مباراة مثيرة",
        "الأهلي يفوز بالتنس الأرضي في مباراة النهائي",
        "مباراة حاسمة في دوري السلة السعودي بين الهلال والنصر",
        "سباق الفورمولا في جدة يتزامن مع مباراة الاتحاد",
        "بطولة الغولف تنطلق في الرياض بعد مباراة الافتتاح",
    ],
)
def test_other_sports_never_enter_football(text):
    assert not matches("football", text)


@pytest.mark.parametrize(
    "text",
    [
        "أبها أمام ٥ اختبارات مصيرية في ٢٢ يوماً للهروب من قاع الدوري السعودي",
        "الهلال يفوز على النصر في مباراة القمة ضمن دوري روشن",
        "تنسيق بين الاتحاد السعودي لكرة القدم والأندية قبل مباراة المنتخب",
        "السماح للأطفال بحضور مباراة صربيا وألمانيا في كرة القدم",
    ],
)
def test_football_stories_still_match(text):
    assert matches("football", text)
