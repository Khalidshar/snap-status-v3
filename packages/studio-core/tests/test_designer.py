import pytest

from studio_core.production import designer

CLIPS = [
    {"file": "a.mp4", "pool": "day", "title": "Pink flowers wave in the breeze"},
    {"file": "b.mp4", "pool": "day", "title": "Aerial view of turquoise waves"},
    {"file": "c.mp4", "pool": "calm", "title": "Sun over hills"},
]
BOX = {"x": 108, "y": 210, "width": 864, "height": 1536}


def plan(*scenes, key="news"):
    return {"key": key, "scenes": [{"role": "hook", "body": "عنوان"}, *scenes, {"role": "cta"}]}


@pytest.fixture(autouse=True)
def measured(monkeypatch):
    monkeypatch.setattr(designer, "luma", lambda path: 140.0)
    monkeypatch.setattr(designer, "bright_spots", lambda path: 150.0)


def test_style_follows_the_pictures():
    picture = {"role": "story", "body": "خبر", "image": "assets/1.jpg"}
    text = {"role": "fact", "body": "معلومة"}
    assert designer.decide(plan(picture, picture), {}, CLIPS)["style"] == "solid"
    assert designer.decide(plan(text, text), {}, CLIPS)["style"] == "footage"
    assert designer.decide(plan(picture, text), {}, CLIPS)["style"] == "mix"


def test_backdrop_fits_the_topic_and_dims_for_its_brightness():
    sea = plan({"role": "story", "body": "السفن تعود إلى الميناء"})
    fashion = plan({"role": "story", "body": "فستان جديد في عرض الموضة"})
    assert designer.decide(sea, {"kind": "news"}, CLIPS)["footage"] == "b.mp4"
    chosen = designer.decide(fashion, {"kind": "news"}, CLIPS)
    assert chosen["footage"] == "a.mp4"
    assert chosen["dim"] == 0.5 and chosen["reasons"]


def test_same_content_same_decision():
    p = plan({"role": "fact", "body": "معلومة"})
    assert designer.decide(p, {}, CLIPS) == designer.decide(p, {}, CLIPS)


def frame(edge, inner):
    row = bytes(inner) * BOX["x"] + bytes(edge) * 40 + bytes(inner) * (1080 - BOX["x"] - 40)
    return row * 1920


def test_bars_are_refused_but_a_dark_backdrop_or_green_is_not():
    picture = (180, 120, 60)
    assert designer.bar(frame((0, 0, 0), picture), BOX, BOX["x"] + 6, 80) == "black"
    assert designer.bar(frame((90, 90, 90), picture), BOX, BOX["x"] + 6, 80) == "grey"
    assert designer.bar(frame((4, 26, 18), picture), BOX, BOX["x"] + 6, 80) is None
    assert designer.bar(frame((3, 3, 3), (4, 4, 4)), BOX, BOX["x"] + 6, 80) is None


def test_bright_spots_behind_text_raise_the_dimming(monkeypatch):
    monkeypatch.setattr(designer, "bright_spots", lambda path: 250.0)
    chosen = designer.decide(plan({"body": "خبر"}), {}, CLIPS)
    assert chosen["dim"] == 0.52 and any("بقع فاتحة" in r for r in chosen["reasons"])
    monkeypatch.setattr(designer, "bright_spots", lambda path: 400.0)
    assert designer.decide(plan({"body": "خبر"}), {}, CLIPS)["dim"] == designer.MAX_DIM


def test_designer_owns_type_size_pages_and_key_words():
    short = {"body": "الرياض تستضيف الحدث"}
    long_ = {"body": "كلمة " * 40}
    quoted = {"body": "أطلقت الشركة «المنصة الجديدة» اليوم في جدة والنصر يفوز"}
    t = designer.decide(plan(short, long_, quoted), {}, CLIPS)["type"]
    assert t["font"] == "body"
    hook, a, b, c, cta = t["cards"]
    assert hook == {} and cta == {}
    assert a["size"] == 84 and a["pages"] == 1 and a["keys"] == ["الرياض"]
    assert b["size"] == 72 and b["pages"] == 2 and b["keys"] == []
    assert c["keys"] == ["المنصة الجديدة", "جدة"]


def test_sacred_clips_get_no_type_decisions():
    p = {**plan({"body": "الرياض"}, key="hadith"), "effects": False}
    t = designer.decide(p, {}, CLIPS)["type"]
    assert all(card == {} for card in t["cards"])
