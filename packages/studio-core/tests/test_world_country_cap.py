from studio_core.production.prepare_news import country_cap, desk_order, story_country


def story(title: str, detail: str = "") -> dict:
    return {"title": title, "detail": detail}


def test_country_from_headline_first():
    assert story_country(story("واشنطن تتهم طهران بتسخير ميليشيات في العراق")) == "usa"
    assert story_country(story("الأمن العراقي يدخل جرف الصخر")) == "iraq"
    assert story_country(story("خبر بلا دولة", "انتخابات في فرنسا اليوم")) == "france"
    assert story_country(story("علماء يكتشفون نجمًا جديدًا")) == ""


def test_world_keeps_at_most_two_per_country_up_front():
    items = [
        story("العراق يعيد تفعيل مسار بري لتصدير النفط"),
        story("الأمن العراقي يدخل جرف الصخر"),
        story("العراق: مقتل ثلاثة في كركوك"),
        story("واشنطن تتهم طهران بتسخير ميليشيات"),
        story("زلزال يضرب اليابان"),
    ]
    ordered = desk_order("world", items, [])
    countries = [story_country(i) for i in ordered[:4]]
    assert countries.count("iraq") == 2
    assert ordered[-1]["title"].startswith("العراق: مقتل")  # the third Iraq story waits


def test_cap_keeps_everything_when_there_is_nothing_else():
    items = [story("العراق خبر أول"), story("العراق خبر ثان"), story("العراق خبر ثالث")]
    assert len(country_cap(items)) == 3
