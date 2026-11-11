"""«كرة القدم» is football only: other sports are excluded in any written form (11 Nov 2026)."""

import pytest

from studio_core.production.prepare import Factory
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


@pytest.mark.parametrize(
    "title, detail",
    [
        (
            "رئيس وزراء إسبانيا يشعل فتيل صراع جديد مع المغرب",
            "الخلاف السياسي يتصاعد بين البلدين اللذين يستضيفان كأس العالم",
        ),
        ("أزمة دبلوماسية بين بلدين", "يلعب المنتخبان مباراة في كأس العالم"),
        ("عقوبات جديدة في النزاع الحدودي", "يأتي ذلك قبل انطلاق كأس العالم"),
        ("رئيس وزراء إسبانيا يشعل صراعا مع المغرب بشأن كأس العالم", "تصريحات سياسية"),
    ],
)
def test_political_subject_cannot_qualify_through_incidental_football(title, detail):
    assert not Factory.editorial_match("football", title, "ar", detail=detail)
    assert not matches("football", title)


@pytest.mark.parametrize(
    "title, detail",
    [
        ("رئيس الوزراء يدعم استضافة كأس العالم", "تنظيم البطولة في ملاعب جديدة"),
        ("قرعة كأس العالم بحضور رئيس الحكومة", "تفاصيل المنتخبات المشاركة"),
        ("مدرب المنتخب يعلن تشكيل مباراة النهائي", "رئيس الوزراء يحضر المباراة"),
        ("صراع التأهل بين الهلال والنصر في الدوري السعودي", "أخبار كرة القدم"),
        ("أزمة إصابة لاعب برشلونة قبل مباراة الدوري", "التفاصيل الطبية من النادي"),
    ],
)
def test_genuine_football_focus_survives_political_or_conflict_words(title, detail):
    assert Factory.editorial_match("football", title, "ar", detail=detail)
    assert matches("football", title)
