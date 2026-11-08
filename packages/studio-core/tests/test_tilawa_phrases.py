"""«تلاوة» (owner 8 Nov 2026): phrases only point into the verified ayah, timed by the reciter."""

import pytest

from studio_core.production.model import ayah_words, check_phrases, phrases_for
from studio_core.religious.quran_foundation import exact_ayah


def timing(text: str, step: int = 500) -> list[list[int]]:
    return [[i, i + 1, i * step, i * step + step - 10] for i in range(len(ayah_words(text)))]


def scene_for(surah: int, ayah: int) -> dict:
    text = exact_ayah(surah, ayah)
    seconds = len(ayah_words(text)) * 0.5 + 0.4
    return {
        "body": text,
        "audio_duration": seconds,
        "phrases": phrases_for(text, timing(text), seconds),
    }


def test_phrases_cover_every_word_once_in_order():
    s = scene_for(18, 1)
    words = ayah_words(s["body"])
    covered = [i for f in s["phrases"] for i in range(f["words"][0], f["words"][1] + 1)]
    assert covered == list(range(len(words)))
    assert all(f["words"][1] - f["words"][0] < 3 for f in s["phrases"])
    check_phrases(s)


def test_pause_mark_closes_a_phrase():
    s = scene_for(18, 1)  # ends its first sentence with a pause mark
    for f in s["phrases"][:-1]:
        last = ayah_words(s["body"])[f["words"][1]]
        assert f["words"][1] - f["words"][0] == 2 or any(c in last for c in "ۖۗۘۙۚۛۜ")


def test_tampered_phrases_are_refused():
    s = scene_for(112, 1)
    s["phrases"][0]["words"] = [1, 1]
    with pytest.raises(ValueError, match="LEGACY_PHRASE_OUTSIDE_AYAH"):
        check_phrases(s)
    s = scene_for(112, 1)
    s["phrases"][-1]["end"] = s["audio_duration"] + 5
    with pytest.raises(ValueError, match="LEGACY_PHRASE_TIMING_INVALID"):
        check_phrases(s)


def test_word_count_must_match_the_reciter_timing():
    text = exact_ayah(112, 1)
    with pytest.raises(ValueError, match="LEGACY_WORD_TIMING_MISMATCH"):
        phrases_for(text, timing(text)[:-1], 3.0)
