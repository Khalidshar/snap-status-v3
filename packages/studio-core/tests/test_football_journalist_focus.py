"""Owner's 12 Nov live headline must not enter either football bulletin."""

import time
from dataclasses import replace
from email.utils import formatdate
from html import escape

import pytest

from studio_core.production.prepare import Factory
from studio_core.sources.news import football_story, news_sources

HEADLINE = "مرور 500 يوم منذ سجن الصحافي الفرنسي كريستوف غليز في الجزائر"
DETAIL = "أقيمت مباراة تضامنية لكرة القدم للمطالبة بالإفراج عنه."
SPORT = "الهلال يتأهل إلى نهائي دوري أبطال آسيا بعد فوزه على منافسه"
SECOND = "مانشستر سيتي يعلن موعد عودة هالاند إلى التدريبات"


@pytest.mark.parametrize("app", ["snap-bot-v2", "status-bot-v2"])
def test_reported_journalist_story_is_excluded_and_genuine_football_is_kept(
    app, tmp_path, monkeypatch
):
    source = replace(news_sources("football")[0], id="fixture", url="https://fixture.example/rss")
    stories = [(HEADLINE, DETAIL * 3), (SPORT, SPORT * 3), (SECOND, SECOND * 3)]
    body = (
        "<rss><channel>"
        + "".join(
            f"<item><title>{escape(title)}</title><link>https://fixture.example/{n}</link>"
            f"<pubDate>{formatdate(time.time() - 60 - n, usegmt=True)}</pubDate>"
            f"<description>{escape(detail)}</description></item>"
            for n, (title, detail) in enumerate(stories)
        )
        + "</channel></rss>"
    )
    monkeypatch.setattr("studio_core.sources.news.news_sources", lambda *a, **k: (source,))
    monkeypatch.setattr("studio_core.sources.panel.approved", lambda *a: ())
    factory = Factory(
        app, "football", "a" * 32, tmp_path / "input", client=object(), state=tmp_path / "state"
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
    scenes = factory.news()
    assert [scene["head"] for scene in scenes] == [SPORT, SECOND]


def test_headline_and_details_are_only_read_not_rewritten():
    before = HEADLINE, DETAIL
    assert not football_story(HEADLINE, DETAIL)
    assert before == (HEADLINE, DETAIL)


@pytest.mark.parametrize("title", [SPORT, "صحافي فرنسي يكشف موعد عودة مهاجم برشلونة إلى المباريات"])
def test_genuine_sporting_subject_is_not_removed(title):
    assert football_story(title)
