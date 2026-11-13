"""Synthetic fashion sources and explicitly reviewed test images; no live network."""

import copy
import email.utils
import hashlib
import json
import time

import pytest

from studio_core.network.client import NetworkResponse
from studio_core import commands
from studio_core.production.fashion_policy import KEYS, record_review
from studio_core.production.model import budget, canonical, sha, validate
from studio_core.production.prepare import Factory
from studio_core.sources import for_command
from studio_core.weather import content_hash

TITLES = {
    "style": "ديور تطلق مجموعة جديدة",
    "style_women": "حقائب شانيل الجديدة للمرأة",
    "style_men": "أرماني تطلق أزياء رجالية",
    "style_perfume": "عطر برادا الجديد",
    "style_couples": "هدايا غوتشي للأزواج",
    "style_kids": "ملابس بربري للأطفال",
}
IMAGE = b"synthetic-reviewed-picture-only"
HOST = "https://ar.harpersbazaararabia.com"
PARAGRAPH = "تفاصيل جديدة من نص المصدر الأصلي عن المواد والتصميم والإصدار الجديد ومعلومات موثقة للقارئ. "


class FashionSources:
    def __init__(self, key, count=1):
        self.key, self.count = key, count
        self.source = next(s for s in for_command("style") if s.id == "bazaar-ar")
        self.pub = email.utils.formatdate(time.time() - 60, usegmt=True)

    def get(self, url):
        if url == self.source.url:
            stories = "".join(
                "<item><title>" + TITLES[self.key] + str(n) + "</title><link>"
                + self.article(n) + "</link><pubDate>" + self.pub + "</pubDate>"
                + "<description>" + PARAGRAPH + "</description>"
                + '<enclosure type="image/jpeg" url="' + self.image(n) + '"/></item>'
                for n in range(self.count)
            )
            raw = ("<rss><channel>" + stories + "</channel></rss>").encode()
        elif url.startswith(HOST + "/fashion/"):
            raw = ("<article>" + "".join(
                "<h2>" + head + "</h2><p>" + head + " " + PARAGRAPH * 3 + "</p>"
                for head in ("التصميم", "تفاصيل الإصدار", "المواد")
            ) + "</article>").encode()
        elif url.startswith(HOST + "/images/"):
            raw = IMAGE
        elif "free-stock-music.com/music/" in url:
            raw = b"synthetic-audio-not-a-real-recording"
        else:
            raise ValueError("TEST_NETWORK_NOT_PERMITTED")
        return NetworkResponse(200, "application/octet-stream", raw, sha(raw))

    def article(self, n):
        return HOST + "/fashion/" + self.key + "/" + str(n)

    def image(self, n):
        return HOST + "/images/" + self.key + "/" + str(n) + ".jpg"


class FixtureFactory(Factory):
    def picture(self, url, base, credit):
        # Image admission is exercised separately; tests never claim real image rendering.
        return self.save(IMAGE, ".jpg", url, credit)


def factory(tmp_path, app, key, client, approve=True):
    state = tmp_path / app / "state"
    sample = tmp_path / (app + "-image.jpg")
    sample.write_bytes(IMAGE)
    if approve:
        for n in range(client.count):
            record_review(
                state / "fashion-image-reviews.jsonl", sample, client.image(n), client.article(n),
                approve=True, apply=True,
            )
    return FixtureFactory(
        app, key, "a" * 32, tmp_path / app / key, client=client, state=state,
    )


@pytest.mark.parametrize("app", ["snap-bot-v2", "status-bot-v2"])
@pytest.mark.parametrize("key", sorted(KEYS))
def test_six_commands_each_make_their_own_source_bound_video_plan(tmp_path, monkeypatch, app, key):
    monkeypatch.setattr("studio_core.production.prepare.duration", lambda path: 2.0)
    f = factory(tmp_path, app, key, FashionSources(key))
    value = f.build()
    plan = validate(value, assets=f.root)
    assert plan["key"] == key
    low, high, _ = budget(app, key)
    assert low <= plan["duration"] <= high
    body = [s for s in plan["scenes"] if s["role"] != "cta"]
    assert [s["role"] for s in body].count("headline") == 1
    assert sum(s["role"] == "point" for s in body) >= 2
    assert all(s["source"] == "بازار العربية" and s["fashion_review"] for s in body)
    assert all(s["published_label"] and ":" not in s["published_label"] for s in body)


@pytest.mark.parametrize("app", ["snap-bot-v2", "status-bot-v2"])
def test_three_stories_make_a_bulletin_in_both_bots(tmp_path, monkeypatch, app):
    monkeypatch.setattr("studio_core.production.prepare.duration", lambda path: 2.0)
    monkeypatch.setattr("studio_core.production.parsing.same_topic", lambda *args: False)
    f = factory(tmp_path, app, "style", FashionSources("style", 3))
    value = f.build()
    plan = validate(value, assets=f.root)
    assert len([s for s in plan["scenes"] if s["role"] == "story"]) == 3


def test_unreviewed_images_leave_evidence_without_consuming_a_story(tmp_path):
    f = factory(tmp_path, "snap-bot-v2", "style", FashionSources("style"), approve=False)
    with pytest.raises(ValueError, match="FASHION_NO_APPROVED"):
        f.fashion()
    evidence = json.loads((f.root / "fashion-image-candidates.json").read_bytes())
    assert evidence[0]["code"] == "FASHION_IMAGE_REVIEW_REQUIRED"
    assert evidence[0]["image_sha256"] == hashlib.sha256(IMAGE).hexdigest()
    assert not f.used(FashionSources("style").article(0), reserve=False)
    assert not (f.root / "content.json").exists()


def test_the_existing_fashion_section_has_exactly_six_shared_commands():
    rows = commands.section_rows("fashion")
    assert [r.key for r in rows] == list(TITLES)
    for row in rows:
        assert commands.parse(row.label)["command"].key == row.key
        assert row.key in KEYS
        assert any(s.status == "approved" for s in for_command(row.key))


def test_local_brand_in_article_is_rejected_before_picture_admission(tmp_path):
    client = FashionSources("style")
    original = client.get

    def get(url):
        reply = original(url)
        if url.startswith(HOST + "/fashion/"):
            raw = reply.body.replace(
                b"</article>",
                "<p>منتج من هدى بيوتي يظهر في تفاصيل هذه القصة ضمن نص المقال الأصلي.</p></article>".encode(),
            )
            return NetworkResponse(200, reply.content_type, raw, sha(raw))
        return reply

    client.get = get
    f = factory(tmp_path, "snap-bot-v2", "style", client)
    with pytest.raises(ValueError, match="FASHION_NO_APPROVED"):
        f.fashion()
    assert not any(a["file"].endswith(".jpg") for a in f.assets)


def test_changed_image_is_rejected_even_if_outer_content_is_resealed(tmp_path, monkeypatch):
    monkeypatch.setattr("studio_core.production.prepare.duration", lambda path: 2.0)
    f = factory(tmp_path, "snap-bot-v2", "style", FashionSources("style"))
    value = f.build()
    bad = copy.deepcopy(value)
    plan = bad["legacy_production"]
    image = next(a for a in plan["assets"] if a["file"] == plan["scenes"][0]["image"])
    raw = b"changed-synthetic-image"
    (f.root / image["file"]).write_bytes(raw)
    image["sha256"] = sha(raw)
    bad["fact_id"] = "legacy-" + sha(canonical(plan))[:32]
    bad["content_hash"] = content_hash(bad)
    with pytest.raises(ValueError, match="FASHION_IMAGE_REVIEW_CHANGED"):
        validate(bad, assets=f.root)
