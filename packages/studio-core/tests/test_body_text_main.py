from studio_core.production.parsing import body_text

PARA = "هذه فقرة من نص المقال الأصلي وهي طويلة بما يكفي لتُحسب فقرة حقيقية."


def test_main_is_used_when_there_is_no_article_and_promos_after_it_are_dropped():
    page = (
        "<html><body><main><h1>العنوان</h1>"
        f"<p>{PARA}</p>"
        '<section><a href="#end-of-recommendations">تخطى</a><h2>قصص مقترحة</h2>'
        "<p>خبر مقترح لا علاقة له بالمقال ويجب ألا يظهر في النص النهائي أبدًا.</p></section>"
        f"<h2>عنوان فرعي</h2><p>{PARA} ثانية</p></main>"
        "<section><h2>اكتشف المزيد</h2>"
        "<p>جماعة تعلن استهداف منشأة نفطية ومجلس الأمن يمدد العقوبات قبل 29 دقيقة</p></section>"
        "<section><h2>الأخبار الرئيسية</h2><p>خبر رئيسي آخر من خارج المقال تمامًا للتجربة.</p>"
        "</section></body></html>"
    )
    text = body_text(page)
    assert PARA in text
    assert "## عنوان فرعي" in text
    for leaked in ("اكتشف المزيد", "الأخبار الرئيسية", "قصص مقترحة", "استهداف منشأة"):
        assert leaked not in text


def test_article_still_wins_over_main():
    page = f"<main><p>{PARA} خارج</p><article><p>{PARA}</p></article></main>"
    assert body_text(page) == PARA
