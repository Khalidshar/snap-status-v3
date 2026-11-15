"""Exercise the command_news collector used before rendering in Telegram/WhatsApp."""

import email.utils
import hashlib
import time
from datetime import UTC, datetime

import pytest

from studio_core.production.fashion_policy import KEYS
from studio_core.production import enabled
from studio_core.network.client import NetworkResponse
from studio_core.sources import for_command
from studio_core.sources.news import collect, digest, validate, validate_delivery, NewsUnavailable


class Feed:
    def __init__(self, title="Michael Kors dress for her"):
        self.title = title
        self.source = next(s for s in for_command("style") if s.id == "vogue")

    def get(self, url):
        if url != self.source.url:
            raise OSError("not a fixture source")
        date = email.utils.formatdate(time.time() - 60, usegmt=True)
        raw = (f"<rss><channel><item><title>{self.title}</title>"
               "<link>https://www.vogue.com/article/real-test-story</link>"
               f"<pubDate>{date}</pubDate></item></channel></rss>").encode()
        return NetworkResponse(200, "application/rss+xml", raw, hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("key", sorted(KEYS))
def test_each_command_collects_a_current_review_or_uses_new_brands(key, monkeypatch):
    monkeypatch.delenv("MADAR_LEGACY_PRODUCTION", raising=False)
    trace = []
    result = collect(key, Feed(), time.time(), source_ids=frozenset({"vogue"}), diagnostics=trace)
    content = result["content"]
    validate(content, time.time())
    assert content["headline"] == "Michael Kors dress for her"
    assert content["command"] == key
    assert content["fashion_desk"] == ("style_women" if key == "style_women" else "style")
    assert content["requires_owner_review"] is True
    assert enabled(key) and not enabled("world")
    assert next(r for r in trace if r["source_id"] == "vogue")["fallback_accepted"] == 1
    with pytest.raises(ValueError, match="NEWS_TRANSLATION_REVIEW_REQUIRED"):
        validate_delivery(content, time.time())
    bad = {**content, "requires_owner_review": False}
    bad["content_hash"] = digest(bad)
    with pytest.raises(ValueError):
        validate(bad, time.time())


def test_seen_and_predicate_are_not_bypassed_by_fallback():
    client = Feed()
    first = collect("style", client, time.time(), source_ids=frozenset({"vogue"}))["content"]
    for kwargs in ({"seen": frozenset({first["fingerprint"]})}, {"predicate": lambda title: False}):
        trace = []
        with pytest.raises(NewsUnavailable):
            collect("style_kids", client, time.time(), source_ids=frozenset({"vogue"}), diagnostics=trace, **kwargs)
        row = next(r for r in trace if r["source_id"] == "vogue")
        assert row["fallback_accepted"] == 0


def test_local_brand_does_not_enter_either_collector_pass():
    with pytest.raises(NewsUnavailable):
        collect("style_kids", Feed("Dior with Huda Beauty dress"), time.time(), source_ids=frozenset({"vogue"}))


@pytest.mark.parametrize("key", sorted(KEYS))
def test_render_worker_uses_the_production_factory_for_every_fashion_key(monkeypatch, key):
    from studio_core.presentation.did_you_know import choose_fact
    from studio_core.production import pipeline

    monkeypatch.delenv("MADAR_LEGACY_PRODUCTION", raising=False)
    expected = {"fixture": key}
    monkeypatch.setattr(pipeline, "choose", lambda repo, job, token, attempt, kind: expected)
    content, events = choose_fact(None, "job", "token", 1, kind=key)
    assert content is expected and events == []


def test_arabic_website_and_latin_house_name_work_without_loosening_world():
    source = next(s for s in for_command("style") if s.id == "laha")
    article = source.url + "fashion/" + "a" * 45
    title = "حقيبة Michael Kors جديدة للمرأة"

    class Website:
        def get(self, url):
            if url == source.url:
                raw = f'<a href="{article}">article</a>'.encode()
            elif url == article:
                published = datetime.fromtimestamp(time.time() - 60, UTC).isoformat()
                raw = (f'<meta property="og:title" content="{title}">'
                       f'<script>{{"datePublished":"{published}"}}</script>').encode()
            else:
                raise OSError("not a fixture source")
            return NetworkResponse(200, "text/html", raw, hashlib.sha256(raw).hexdigest())

    content = collect("style_men", Website(), time.time(), source_ids=frozenset({"laha"}))["content"]
    validate_delivery(content, time.time())
    assert content["headline"] == title and content["language"] == "ar"
    assert content["fashion_desk"] == "style" and content["command"] == "style_men"
