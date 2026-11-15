"""Each requested desk remains bound to its request when the approved fallback is used."""

import pytest

from studio_core.production.fashion_policy import section
from studio_core.production.model import sha


@pytest.mark.parametrize(
    ("title", "desk"),
    [
        ("Chanel dress for her", "style_women"),
        ("Dior bag for a woman", "style_women"),
        ("Chanel women collection", "style_women"),
        ("Dior man collection", "style_men"),
        ("Armani menswear", "style_men"),
        ("Prada perfume fragrance", "style_perfume"),
        ("Burberry children collection", "style_kids"),
        ("Gucci couple wedding collection", "style_couples"),
        ("حقيبة شانيل للمرأة", "style_women"),
        ("عطر برادا", "style_perfume"),
        ("هدايا غوتشي للأزواج", "style_couples"),
        ("ملابس بربري للأطفال", "style_kids"),
    ],
)
def test_arabic_and_english_desk_words(title, desk):
    assert section(title) == desk


def test_diagnostic_counts_each_filter_and_keeps_unknown_fetches_explicit():
    from studio_core.production.fashion_diagnostic import diagnose, funnel

    good = {
        "title": "Dior dress",
        "detail": "",
        "url": "https://example.test/a",
        "age_ok": True,
        "language_ok": True,
    }
    items = [
        good,
        {**good, "age_ok": False},
        {**good, "language_ok": False},
        {**good, "title": "عطر العربية للعود"},
        {**good, "title": "Dior fragrance"},
    ]
    rows = funnel(items, lambda item: True)
    women = next(r for r in rows if r["command"] == "style_women")
    stages = ("raw", "age", "language", "brand", "desk", "image", "deduplicated")
    assert [women[k] for k in stages] == [5, 4, 3, 2, 1, 1, 1]
    kids = next(r for r in rows if r["command"] == "style_kids")
    assert kids["desk"] == 0 and kids["fallback_pool"] == 2

    class Offline:
        def get(self, url):
            raise OSError("private detail not to be printed")

    report = diagnose(Offline())
    assert report["complete"] is False
    assert all(r["result"] == "FETCH_OR_PARSE_FAILED" and "raw" not in r for r in report["sources"])
    assert "private detail" not in str(report)


def test_diagnostic_public_feed_success_and_cached_picture(monkeypatch):
    import email.utils
    import time

    from studio_core.network.client import NetworkResponse
    from studio_core.production import fashion_diagnostic as diagnostic
    from studio_core.sources import for_command

    source = next(s for s in for_command("style") if s.id == "vogue")
    monkeypatch.setattr(diagnostic, "for_command", lambda key: (source,))
    monkeypatch.setattr(diagnostic, "news_sources", lambda key, production: (source,))
    article, image = "https://www.vogue.com/article/test", "https://assets.vogue.com/test.jpg"
    date = email.utils.formatdate(time.time() - 60, usegmt=True)
    rss = (
        f"<rss><channel><item><title>Dior dress</title><link>{article}</link>"
        f"<pubDate>{date}</pubDate><description>Fashion collection</description>"
        "</item></channel></rss>"
    ).encode()
    calls = []

    class Public:
        def get(self, url):
            calls.append(url)
            raw = {
                source.url: rss,
                article: f'<meta property="og:image" content="{image}">'.encode(),
                image: b"\xff\xd8synthetic-test-image",
            }[url]
            return NetworkResponse(200, "application/octet-stream", raw, sha(raw))

    report = diagnostic.diagnose(Public())
    assert report["complete"]
    assert report["sources"][0]["raw"] == 1
    assert report["rows"][0]["deduplicated"] == 1
    assert next(r for r in report["rows"] if r["command"] == "style_women")["image"] == 1
    assert calls == [source.url, article, image]
