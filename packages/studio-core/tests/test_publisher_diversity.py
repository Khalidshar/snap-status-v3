"""Both production apps prefer valid alternative publishers before a third story."""

import time
from collections import Counter
from dataclasses import replace
from email.utils import formatdate
from html import escape

import pytest

from studio_core.production.prepare import Factory
from studio_core.sources.news import news_sources

TITLES = (
    "فرنسا تعلن انتهاء احتجاجات العمال وإعادة فتح الطرق",
    "الصين تطلق مركبة فضائية لاستكشاف الكواكب البعيدة",
    "البرازيل تفتتح محمية طبيعية لحماية الحيوانات البرية",
    "الهند تكشف عن مشروع قطارات سريعة لربط المدن",
    "ألمانيا تطور تقنية جديدة لتحلية مياه البحر",
    "اليابان تفتتح متحفا للفنون والآثار التاريخية",
)


@pytest.mark.parametrize("key", ["news", "football", "markets"])
@pytest.mark.parametrize("app, count", [("snap-bot-v2", 4), ("status-bot-v2", 3)])
@pytest.mark.parametrize("alternative", ["valid", "bad_image", "duplicate", "absent"])
def test_actual_bulletin_selection(key, app, count, alternative, tmp_path, monkeypatch):
    base = news_sources(key)[0]
    sources = tuple(
        replace(base, id=label, name=label, url=f"https://{label}.example/feed")
        for label in ("first", "second")
    )
    titles = {
        "news": TITLES,
        "football": (
            "الهلال يحسم صدارة الدوري بعد فوزه على الشباب",
            "مانشستر سيتي يعلن عودة هالاند إلى التدريبات",
            "برشلونة يتأهل إلى نهائي كأس إسبانيا",
            "منتخب اليابان يعلن قائمة تصفيات كأس العالم",
            "نادي النصر يجدد عقد حارس منتخب السنغال حتى الموسم المقبل",
            "بايرن ميونيخ يضم حارس مرمى جديدًا قبل انطلاق الدوري الألماني",
        ),
        "markets": (
            "أسعار الذهب ترتفع مع هبوط الدولار أمام العملات",
            "سهم أرامكو يسجل مكاسب مع ارتفاع أرباح الشركة",
            "بورصة طوكيو تصعد بدعم نتائج الشركات الصناعية",
            "صندوق النقد الدولي يتوقع تباطؤ التضخم في الأرجنتين",
            "اقتصاد الصين ينمو مع تحسن الصادرات وطلب المصانع",
            "البنك المركزي الأوروبي يخفض سعر الفائدة",
        ),
    }[key]
    first = list(titles[:4])
    second = list(titles[4:])
    if alternative == "duplicate":
        second = first[:2]
    elif alternative == "absent":
        second = []
    now = time.time()
    feeds = {}
    for source, titles in zip(sources, (first, second), strict=True):
        items = []
        for n, title in enumerate(titles):
            # All the first publisher's stories rank before the alternative.
            stamp = now - 60 - n - (100 if source.id == "second" else 0)
            items.append(
                f"<item><title>{escape(title)}</title><link>https://{source.id}.example/{n}"
                f"</link><pubDate>{formatdate(stamp, usegmt=True)}</pubDate>"
                f"<description>{escape('؛ '.join([title] * 3))}</description></item>"
            )
        feeds[source.url] = ("<rss><channel>" + "".join(items) + "</channel></rss>").encode()
    monkeypatch.setattr("studio_core.sources.news.news_sources", lambda *a, **k: sources)
    monkeypatch.setattr("studio_core.sources.panel.approved", lambda *a: ())
    f = Factory(app, key, "a" * 32, tmp_path / "work", client=object(), state=tmp_path / "state")

    def get(url):
        if url not in feeds:
            raise RuntimeError("No synthetic trends feed")
        return feeds[url]

    def article(item):
        item["article"] = "assets/article.html"
        return item["detail"], []

    def picture(item):
        if alternative == "bad_image" and item["source_id"] == "second":
            raise ValueError("No valid image")
        return "assets/image.jpg"

    monkeypatch.setattr(f, "get", get)
    monkeypatch.setattr(f, "article", article)
    monkeypatch.setattr(f, "news_picture", picture)
    scenes = f.news()
    publishers = Counter(scene["source_id"] for scene in scenes)
    assert len(scenes) == count
    if alternative == "valid":
        assert publishers["first"] == 2
        assert publishers["second"] == count - 2
    else:
        assert publishers == {"first": count}
    assert len({scene["source_url"] for scene in scenes}) == count


def test_bbc_domains_are_one_publisher_but_unrelated_hosts_are_distinct():
    from studio_core.production.news_selection import publisher

    assert publisher("https://feeds.bbci.co.uk/ar/rss") == publisher("https://www.bbc.com/ar/x")
    assert publisher("https://www.bbc.co.uk/ar/x") == publisher("https://bbc.com/ar/y")
    assert publisher("https://bbc.com.evil.example/x") != publisher("https://bbc.com/ar/y")
