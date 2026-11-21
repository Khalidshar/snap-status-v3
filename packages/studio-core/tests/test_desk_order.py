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


def test_saudi_bulletin_is_saudi_only():
    # Owner 21 Nov 2026: no foreign story under the Saudi badge; other kingdoms never count.
    stories = [
        *STORIES,
        story("المملكة المتحدة ترفع الفائدة"),
        story("ألاسكا تمنح الديمقراطيين"),
        story("بعد 160 عاما.. ذكاء اصطناعي يحل لغزا في الرياضيات"),
        story("نجم الرياضة الأول في العالم"),
        # Owner 21 Nov 2026: sport belongs to the sports command, even when Saudi.
        story("رئيس الاتحاد السعودي لكرة الطاولة: أولمبياد لوس أنجليس هدف رئيسي"),
        story("توني مهاجم الأهلي يتصدر الهدافين في دوري روشن السعودي"),
        story("المنتخب السعودي يستعد لمباراة الأحد في الرياض"),
        story("جياد فيصل بن خالد تتألق في سباقات «موسم الرياض»"),
        story("الرياض تجمع رياضات الشعوب في مهرجان الإثنوسبورت"),
    ]
    titles = [i["title"] for i in desk_order("saudi", stories, [])]
    assert titles == [
        "السعودية تعلن مشروعا جديدا في الرياض",
        "المملكة تستضيف قمة دولية",
        "أمير الرياض يفتتح معرضا",
    ]


def test_other_bulletins_keep_two_saudi_stories_first():
    for key in ("news", "news_noon", "football", "markets"):
        titles = [i["title"] for i in desk_order(key, STORIES, [])]
        assert titles[:2] == ["السعودية تعلن مشروعا جديدا في الرياض", "المملكة تستضيف قمة دولية"]
        assert titles[2:4] == ["فرنسا تعلن اتفاقا جديدا مع ألمانيا", "اليابان تطلق قمرا صناعيا"]
        # «البرازيل تفوز بالبطولة» is sport: the general bulletins leave it (21 Nov).
        expected = len(STORIES) - (key in {"news", "news_noon"})
        assert len(titles) == expected


def test_general_bulletins_leave_sport_to_the_sports_command():
    # Owner 21 Nov 2026 («اعتمد»): موجز الأخبار and أخبار الظهر carry no sport.
    stories = [*STORIES, story("المنتخب السعودي يستعد لمباراة الأحد في الرياض")]
    for key in ("news", "news_noon"):
        titles = [i["title"] for i in desk_order(key, stories, [])]
        assert "المنتخب السعودي يستعد لمباراة الأحد في الرياض" not in titles
        assert "السعودية تعلن مشروعا جديدا في الرياض" in titles


def test_sports_command_takes_every_sport_and_nothing_else():
    from studio_core.production.prepare_news import NewsPreparation
    from studio_core.sources.news import matches

    match = NewsPreparation.editorial_match
    for title in (
        "رئيس الاتحاد السعودي لكرة الطاولة: أولمبياد لوس أنجليس هدف رئيسي",
        "جياد فيصل بن خالد تتألق في سباقات موسم الرياض",
        "الهلال يفوز في دوري روشن",
        "الرياض تجمع رياضات الشعوب في مهرجان الإثنوسبورت",
    ):
        assert match("sports", title, "ar"), title
        assert matches("sports", title), title
    for title in ("مجلس الوزراء يقر نظاما جديدا في الرياض", "ذكاء اصطناعي يحل لغزا في الرياضيات"):
        assert not match("sports", title, "ar"), title


def test_thumbnail_links_ask_for_the_large_picture():
    # Owner 21 Nov 2026: a 100 px Sky News thumbnail was stretched across the card.
    from studio_core.production.prepare_news import sharper

    sky = "https://images.skynewsarabia.com/images/v1/2026/11/21/1906152/95/53/1-1906152.jpg"
    assert sharper(sky).endswith("/1906152/1200/670/1-1906152.jpg")
    bbc = "https://ichef.bbci.co.uk/ace/ws/240/cpsprodpb/abc.jpg"
    assert sharper(bbc) == "https://ichef.bbci.co.uk/ace/ws/1024/cpsprodpb/abc.jpg"
    big = "https://s.france24.com/media/display/x/w:1024/p:16x9/a.jpg"
    assert sharper(big) == big
