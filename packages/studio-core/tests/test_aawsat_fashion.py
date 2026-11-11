"""The fashion source is the official section, never the site's general RSS feed."""

from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import pytest

from studio_core.production.prepare import Factory
from studio_core.sources import for_command
from studio_core.sources.news import news_sources


def test_only_production_section_adapter_selects_aawsat_fashion():
    source = next(s for s in for_command("style") if s.id == "aawsat-fashion")
    assert source.kind == "website" and source.status == "candidate"
    assert unquote(source.url) == "https://aawsat.com/يوميات-الشرق/لمسات-الموضة"
    assert all(s.id != source.id for s in news_sources("style"))
    assert all(s.id != source.id for s in news_sources("style", production=True))


@pytest.mark.parametrize("app", ["snap-bot-v2", "status-bot-v2"])
def test_section_rejects_general_navigation_foreign_hosts_and_old_stories(
    app, tmp_path, monkeypatch
):
    source = next(s for s in for_command("style") if s.id == "aawsat-fashion")
    section = unquote(source.url)
    fresh, old = section + "/12345-ديور-عطر-جديد", section + "/12346-شانيل-مجموعة-أزياء"
    general = "https://aawsat.com/العالم/12347-جدل-سياسي-حول-الموضة-العالمية"
    foreign = "https://evil.example/يوميات-الشرق/لمسات-الموضة/12348-عطر"
    homepage = "".join(f'<a href="{link}">المزيد</a>' for link in (general, foreign, old, fresh))
    now = datetime.now(UTC)

    def article(stamp):
        return (
            '<meta property="og:title" content="ديور تطلق عطرًا جديدًا ضمن مجموعة العطور">'
            '<script type="application/ld+json">{"datePublished":"'
            + stamp.isoformat()
            + '"}</script>'
            "<article><p>"
            + "تفاصيل عطر ديور الجديد ومجموعة الأزياء العالمية. " * 10
            + "</p></article>"
        )

    pages = {
        unquote(source.url): homepage,
        fresh: article(now),
        old: article(now - timedelta(days=9)),
    }
    calls = []

    def get(url):
        url = unquote(url)
        calls.append(url)
        assert url in pages, "Must not request general RSS, navigation or a foreign publisher"
        return pages[url].encode()

    f = Factory(
        app, "style", "d" * 32, tmp_path / "work", client=object(), state=tmp_path / "state"
    )
    monkeypatch.setattr(f, "get", get)
    candidates = f.website_candidates(source)
    assert [unquote(item["url"]) for item in candidates] == [fresh]
    assert general not in calls and foreign not in calls
    assert all("/feed" not in url for url in calls)
