"""«الأسواق» from a general feed needs an economic headline (owner 12 Nov 2026)."""

import pytest

from studio_core.production.prepare import Factory

OFF_TOPIC = [
    (
        "اختيارات اختصاصيي التغذية... ٤ وجبات خفيفة تساعد على استقرار سكر الدم",
        "يمكن أن يسهم اختيار الوجبات الخفيفة في دعم مستويات السكر ويعود بفائدة على الصحة "
        "مع ارتفاع أسعار الأغذية",
    ),
    (
        "بنين.. \"مدارس الأزواج المثاليين\" مبادرة لتعزيز حقوق النساء داخل الأسرة والمجتمع",
        "يحاول أزواج تغيير العلاقات داخل الأسرة، والقرارات المالية والاقتصادية تمر عبر الزوج",
    ),
]
ECONOMIC = [
    (
        "«ساسكو» السعودية تتحوّل إلى الخسارة بـ5.5 مليون دولار في الربع الثالث",
        "تحولت الشركة السعودية لخدمات السيارات إلى تسجيل صافي خسارة",
    ),
    (
        "النفط يتراجع دون 90 دولاراً للمرة الأولى منذ أغسطس",
        "تراجعت أسعار النفط مع تهدئة المخاوف",
    ),
]


@pytest.mark.parametrize(("title", "detail"), OFF_TOPIC)
def test_general_feed_story_without_economic_headline_is_refused(title, detail):
    assert not Factory.editorial_match("markets", title, "ar", detail=detail, source_id="skynews-ar")


@pytest.mark.parametrize(("title", "detail"), ECONOMIC)
def test_economic_headline_from_a_general_feed_is_kept(title, detail):
    assert Factory.editorial_match("markets", title, "ar", detail=detail, source_id="aawsat")


def test_economy_feeds_keep_the_text_rule():
    title = "كيف سيضبط نظام حماية المستهلك في السعودية العلاقة بين البائع والمشتري؟"
    detail = "يمتد أثر النظام إلى تعزيز الشفافية في السوق والشركات"
    assert Factory.editorial_match(
        "markets", title, "ar", detail=detail, source_id="aawsat-economy"
    )
    assert not Factory.editorial_match("markets", title, "ar", detail=detail, source_id="aawsat")
