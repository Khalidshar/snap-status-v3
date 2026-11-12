"""Destination metadata follows every scene, without altering any source text."""

import copy

import pytest

from studio_core.production.publication import notice, target


@pytest.mark.parametrize(
    "text",
    [
        "رئيس وزراء إسبانيا يشعل صراعا جديدا مع المغرب",
        "مرور 500 يوم منذ سجن الصحافي الفرنسي كريستوف غليز في الجزائر",
        "البرلمان يناقش مشروع قانون الانتخابات",
        "الجيش يعلن وقف إطلاق النار بعد القصف",
        "ولي العهد يبحث العلاقات مع دولة أخرى",
        "ترامب يعلن سياسة جديدة",
    ],
)
def test_political_or_military_material_goes_to_public_story(text):
    value = {"title": "أخبار العالم", "body": text}
    original = copy.deepcopy(value)
    assert target(value) == "PUBLIC_STORY"
    assert notice(value) == "وجهة النشر: القصص العامة"
    assert value == original


@pytest.mark.parametrize(
    "text",
    [
        "الهلال يفوز في مباراة الدوري السعودي",
        "صحافي فرنسي يكشف موعد عودة مهاجم برشلونة إلى المباريات",
        "ديور تطلق مجموعة عطور جديدة",
        "شركة تطور معالجا للحاسوب",
        "انخفاض أسعار النفط وارتفاع أسهم البنوك",
        "حرب الأسعار بين شركات الهواتف",
        "حقائق عن الأسد ملك الغابة",
    ],
)
def test_nonpolitical_content_goes_to_spotlight(text):
    assert target({"title": text}) == "SPOTLIGHT"


def test_one_political_scene_routes_the_whole_bulletin_and_declared_tag_is_not_trusted():
    value = {
        "title": "موجز الأخبار",
        "body": "خبر اقتصادي",
        "publication_target": "SPOTLIGHT",
        "legacy_production": {"scenes": [{"head": "قصف جديد", "body": "تفاصيل الحدث"}]},
    }
    assert target(value) == "PUBLIC_STORY"
    assert target({"kind": "urgent", "body": "تنبيه"}) == "PUBLIC_STORY"


def test_religious_text_is_not_used_as_political_keyword_input():
    value = {"kind": "hadith", "body": "نص ديني اختباري يتضمن كلمة حرب"}
    original = copy.deepcopy(value)
    assert target(value) == "SPOTLIGHT"
    assert value == original


@pytest.mark.parametrize("value", [None, [], {}, {"kind": []}])
def test_missing_content_is_unknown(value):
    assert target(value) == "UNKNOWN"
