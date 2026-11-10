"""«العالم» is the world only; the other bulletins keep Saudi stories first (10 Nov 2026)."""

from studio_core.production.prepare import desk_order


def story(title: str) -> dict[str, str]:
    return {"title": title, "detail": title}


STORIES = [
    story("فرنسا تعلن اتفاقا جديدا مع ألمانيا"),
    story("السعودية تعلن مشروعا جديدا في الرياض"),
    story("اليابان تطلق قمرا صناعيا"),
    story("المملكة تستضيف قمة دولية"),
    story("البرازيل تفوز بالبطولة"),
    story("أمير الرياض يفتتح معرضا"),
]


def test_world_bulletin_has_no_saudi_story():
    titles = [i["title"] for i in desk_order("world", STORIES, ["اليابان"])]
    assert titles == [
        "اليابان تطلق قمرا صناعيا",
        "فرنسا تعلن اتفاقا جديدا مع ألمانيا",
        "البرازيل تفوز بالبطولة",
    ]


def test_other_bulletins_keep_two_saudi_stories_first():
    for key in ("saudi", "news", "news_noon", "football", "markets"):
        titles = [i["title"] for i in desk_order(key, STORIES, [])]
        assert titles[:2] == ["السعودية تعلن مشروعا جديدا في الرياض", "المملكة تستضيف قمة دولية"]
        assert titles[2:4] == ["فرنسا تعلن اتفاقا جديدا مع ألمانيا", "اليابان تطلق قمرا صناعيا"]
        assert len(titles) == len(STORIES)
