"""Official canonical paths, dated stories and source images; no guessed URLs."""

import hashlib
import time
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

import pytest

from studio_core.network.client import NetworkResponse
from studio_core.production.parsing import _img, feed_time
from studio_core.production.prepare import Factory, safe_xml
from studio_core.sources import for_command
from studio_core.sources.audit import audit
from studio_core.sources.news import _link, collect, validate
from studio_core.sources.publisher_feeds import HOME, arriyadiyah


def fixture():
    stamp = datetime.now(ZoneInfo("Asia/Riyadh")).strftime("%Y-%m-%d %H:%M:%S")
    titles = (
        "الهلال يضم مهاجما جديدا",
        "برشلونة يستعد لمباراة الكأس",
        "مانشستر يفوز في الدوري الإنجليزي",
    )
    entries, links = [], []
    for n, title in enumerate(titles, 100):
        entries.append(
            f"<item><id>{n}</id><title>{title}</title><link>{HOME}{n}</link>"
            f"<publication_time>{stamp}</publication_time>"
            f"<main_image>{HOME}media/{n}.png</main_image>"
            f"<description>{title} وتفاصيل الحدث من المصدر.</description></item>"
        )
        links.append(f'<a href="/{n}/{escape(title.replace(" ", "-"))}">الخبر</a>')
    return ("<rss><channel>" + "".join(entries) + "</channel></rss>").encode(), "".join(links)


def test_official_fields_become_standard_fields_with_riyadh_time():
    raw, home = fixture()
    item = next(safe_xml(arriyadiyah(raw, home)).iter("item"))
    assert _img(item) == HOME + "media/100.png"
    assert feed_time(item.findtext("pubDate")) == pytest.approx(time.time(), abs=2)
    source = next(s for s in for_command("football") if s.id == "arriyadiyah")
    assert _link(item.findtext("link"), source).startswith(HOME + "100/")
    assert item.findtext("title") == "الهلال يضم مهاجما جديدا"


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/100/title",
        "https://arriyadiyah.com.evil.example/100/title",
        "https://user@arriyadiyah.com/100/title",
        "http://arriyadiyah.com/100/title",
        "/100",
        "/999/title",
    ],
)
def test_missing_or_foreign_canonical_link_never_becomes_an_article(href):
    raw, _ = fixture()
    assert not list(safe_xml(arriyadiyah(raw, f'<a href="{href}">x</a>')).iter("item"))


def test_malformed_entity_and_oversize_inputs_are_rejected():
    for raw in (b"<rss>", b'<!DOCTYPE rss [<!ENTITY x "a">]><rss/>', b"x" * 2_000_001):
        with pytest.raises(ValueError):
            arriyadiyah(raw, "")


@pytest.mark.parametrize("app", ["snap-bot-v2", "status-bot-v2"])
def test_both_apps_use_normalized_links_dates_and_images(app, tmp_path, monkeypatch):
    raw, home = fixture()
    source = next(s for s in for_command("football") if s.id == "arriyadiyah")
    monkeypatch.setattr("studio_core.sources.news.news_sources", lambda *a, **k: (source,))
    monkeypatch.setattr("studio_core.sources.panel.approved", lambda *a: ())
    f = Factory(
        app, "football", "b" * 32, tmp_path / "work", client=object(), state=tmp_path / "state"
    )

    def get(url):
        if url == source.url:
            return raw
        if url == HOME:
            return home.encode()
        raise RuntimeError("No synthetic trends feed")

    def article(item):
        item["article"] = "assets/article.html"
        return item["detail"], []

    monkeypatch.setattr(f, "get", get)
    monkeypatch.setattr(f, "article", article)
    monkeypatch.setattr(f, "news_picture", lambda item: item["image"])
    scenes = f.news()
    assert len(scenes) == 3
    assert all(
        scene["source_url"].startswith(HOME) and "/" in scene["source_url"][len(HOME) :]
        for scene in scenes
    )
    assert all(scene["image"].startswith(HOME + "media/") for scene in scenes)


def test_collection_and_audit_use_the_same_official_adapter(monkeypatch):
    raw, home = fixture()
    source = next(s for s in for_command("football") if s.id == "arriyadiyah")
    monkeypatch.setattr("studio_core.sources.news.news_sources", lambda *a, **k: (source,))
    monkeypatch.setattr("studio_core.sources.audit.for_command", lambda *a: (source,))

    class Client:
        def get(self, url):
            body = raw if url == source.url else home.encode()
            assert url in {source.url, HOME}
            return NetworkResponse(200, "text/xml", body, hashlib.sha256(body).hexdigest())

    now = time.time()
    content = collect("football", Client(), now)["content"]
    validate(content, now)
    assert content["source_id"] == "arriyadiyah" and content["source_url"].startswith(HOME)
    result = audit("football", Client())
    assert result[0]["result"] == "XML_VERIFIED" and result[0]["valid_items"] == 3
