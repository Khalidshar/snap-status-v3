"""«الأسواق» carries no security story, even one that mentions the economy (12 Nov 2026)."""

import pytest

from studio_core.production.parsing import market_excluded, market_story

SECURITY = [
    "وزير الداخلية السعودي: اعتداءات الحوثيين لا يمكن تبريرها أكد الأمير عبد العزيز بن سعود "
    "بن نايف وزير الداخلية السعودي أن استهداف الميليشيا الحوثية للأعيان المدنية والاقتصادية "
    "في المملكة لا يمكن تبريره.",
    "انفجار قرب ميناء نفطي يرفع أسعار النفط",
    "وزارة الدفاع تعلن اعتراض هجوم استهدف منشأة اقتصادية",
    "مقتل مسلحين في اشتباكات قرب حقل نفط",
    "تفجيرات إرهابية تضرب العاصمة وتهبط بالبورصة",
]
MARKETS = [
    "السعودية تتقدم عالمياً بمعايير الذكاء الاصطناعي في تصنيف التنافسية الرقمية والاقتصاد",
    "«ديلويت»: طموحات الذكاء الاصطناعي تتجاوز قدرات ٤٢ % من قادة المالية في الشركات",
    "أرباح «السعودية للطاقة» ترتفع 0.4 % إلى 1.43 مليار دولار في الربع الثالث",
    "النفط يتراجع دون 90 دولاراً للمرة الأولى منذ أغسطس",
    "البنك المركزي يبقي الفائدة دون تغيير واستهداف التضخم عند 2 %",
    "تداول السعودية: المؤشر يرتفع مع صعود أسهم البنوك",
]


@pytest.mark.parametrize("text", SECURITY)
def test_security_stories_never_enter_markets(text):
    assert market_excluded(text)
    assert not market_story(text)


@pytest.mark.parametrize("text", MARKETS)
def test_market_stories_still_enter(text):
    assert not market_excluded(text)
    assert market_story(text)
