from studio_core.production.parsing import clean


def test_source_emoji_are_dropped_and_text_kept():
    assert clean("صورك الأخيرة قد تظهر داخل المحادثة 📷") == "صورك الأخيرة قد تظهر داخل المحادثة"
    assert clean("ارتفاع 20%! ✅ في 2026") == "ارتفاع 20%! في 2026"
