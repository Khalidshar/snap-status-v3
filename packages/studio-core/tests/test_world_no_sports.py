"""World is global general news; sport needs its own command."""

import time
from dataclasses import replace
from email.utils import formatdate
from html import escape

import pytest

from studio_core.production.prepare import Factory
from studio_core.sources.news import matches, news_sources

SPORT = "منتخب الصين يتأهل إلى نهائي كأس العالم لكرة القدم"
POLITICS = "روسيا تعلن مبادرة دبلوماسية جديدة لوقف الحرب في أوكرانيا"
ECONOMY = "البنك المركزي الأوروبي يخفض الفائدة لدعم اقتصاد أوروبا"


@pytest.mark.parametrize("app", ["snap-bot-v2", "status-bot-v2"])
def test_world_bulletin_rejects_sports_but_keeps_global_news(app, tmp_path, monkeypatch):
    source = replace(news_sources("world")[0], id="fixture", url="https://fixture.example/rss")
    titles = [SPORT, POLITICS, ECONOMY]
    body = (
        "<rss><channel>"
        + "".join(
            f"<item><title>{escape(title)}</title><link>https://fixture.example/{n}</link>"
            f"<pubDate>{formatdate(time.time() - 60 - n, usegmt=True)}</pubDate>"
            f"<description>{escape('؛ '.join([title] * 3))}</description></item>"
            for n, title in enumerate(titles)
        )
        + "</channel></rss>"
    )
    monkeypatch.setattr("studio_core.sources.news.news_sources", lambda *a, **k: (source,))
    monkeypatch.setattr("studio_core.sources.panel.approved", lambda *a: ())
    factory = Factory(
        app, "world", "a" * 32, tmp_path / "input", client=object(), state=tmp_path / "state"
    )

    def get(url):
        if url != source.url:
            raise RuntimeError("Synthetic fixture has no trends feed")
        return body.encode()

    def article(item):
        item["article"] = "assets/article.html"
        return item["detail"], []

    monkeypatch.setattr(factory, "get", get)
    monkeypatch.setattr(factory, "article", article)
    monkeypatch.setattr(factory, "news_picture", lambda item: "assets/picture.jpg")
    assert {scene["head"] for scene in factory.news()} == {POLITICS, ECONOMY}


@pytest.mark.parametrize(
    "title",
    [
        SPORT,
        "منتخب فرنسا يتوج ببطولة العالم",
        "لاعب الصين يتوج ببطولة التنس العالمية",
        "روسيا تستضيف الألعاب الأولمبية",
    ],
)
def test_direct_world_source_filter_also_rejects_sports(title):
    assert not matches("world", title)
    assert not Factory.editorial_match("world", title, "ar")


@pytest.mark.parametrize(
    "title",
    [
        "الاتحاد الأوروبي يوسع مساعداته الإنسانية إلى أوكرانيا",
        "الهلال الأحمر يرسل مساعدات جديدة إلى الصين",
        "الرئيس المنتخب في فرنسا يعلن خطته السياسية تجاه روسيا",
        "الرئيس المنتخب الروسي يعلن خطته الجديدة لمستقبل روسيا",
        "الصين تصدر تقريرا دوريا عن اقتصادها",
        "الصين تخفض أسعار سلة الغذاء مع زيادة الإنتاج",
        "ألمانيا تدعم صناعة الدراجات لتوسيع النقل المستدام",
    ],
)
def test_non_sporting_uses_of_team_like_words_remain_global_news(title):
    assert matches("world", title)
    assert Factory.editorial_match("world", title, "ar")


def test_political_headline_is_not_removed_for_incidental_world_cup_in_details():
    title = "الصين تعلن تفاهمات دبلوماسية جديدة مع دول أوروبا"
    detail = "وشملت المناقشات ملف استضافة كأس العالم لكرة القدم."
    assert Factory.editorial_match("world", title, "ar", detail=detail)
