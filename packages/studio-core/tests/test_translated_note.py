"""A machine-translated clip is flagged to the owner (11 Nov 2026: «أعتمد كل مقطع»)."""

from studio_core.production.caption import TRANSLATED_NOTE, machine_translated


def clip(*scenes):
    return {"legacy_production": {"scenes": list(scenes)}}


def test_only_clips_with_translated_scenes_are_flagged():
    assert machine_translated(clip({"body": "نص"}, {"translations": {"body": "source text"}}))
    assert not machine_translated(clip({"body": "نص"}, {"translations": {}}))
    assert not machine_translated(clip())
    assert not machine_translated({"title": "هل تعلم؟"})
    assert not machine_translated(None)


def test_the_notice_tells_the_owner_to_review_before_posting():
    assert "مترجم" in TRANSLATED_NOTE and "راجع" in TRANSLATED_NOTE
