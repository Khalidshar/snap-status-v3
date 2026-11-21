"""News summaries end at a sentence end, not mid-sentence with «…» (owner 21 Nov 2026)."""

from studio_core.production.prepare_news import whole_sentences

FIRST = "أعلنت وزارة الصحة اليوم عن افتتاح مستشفى جديد في مدينة الرياض بسعة كبيرة."
SECOND = " ويضم المستشفى أقسامًا متخصصة في أمراض القلب والأعصاب والطوارئ ويخدم سكان الأحياء الشمالية"


def test_long_text_ends_at_last_whole_sentence():
    text = FIRST + SECOND + " وما حولها من المناطق القريبة."
    result = whole_sentences(text, len(FIRST) + 20)
    assert result == FIRST
    assert not result.endswith("…")


def test_short_whole_text_is_kept():
    assert whole_sentences(FIRST, 230) == FIRST


def test_feed_ellipsis_tail_is_dropped_back_to_a_sentence():
    for tail in ("…", "...", " […]", " [...]"):
        assert whole_sentences(FIRST + SECOND + tail, 230) == FIRST


def test_no_whole_sentence_falls_back_to_the_old_cut():
    text = "خبر " * 80
    result = whole_sentences(text, 100)
    assert result.endswith("…") and len(result) <= 101


def test_result_is_verbatim_part_of_the_source():
    text = FIRST + SECOND + "."
    assert whole_sentences(text, 120) in text
