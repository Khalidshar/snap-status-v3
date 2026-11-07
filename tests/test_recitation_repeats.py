from studio_core.production.model import REPEAT_GAP, recitation_repeats, scene, timed


def clip():
    scenes = [
        scene("عنوان", 3.2, role="hook"),
        scene("آية ١", 6.0 + 0.8 + 14.0, verse=[18, 1], audio="a1.mp3", audio_duration=6.0),
        scene("آية ٢", 4.0 + 0.8 + 3.0, verse=[18, 2], audio="a2.mp3", audio_duration=4.0),
        scene("السعدي", 20.0, role="saadi", saadi=[18, [1, 2]]),
        scene("خاتمة", 5.0, role="cta"),
    ]
    return timed(scenes)


def test_whole_recitations_fill_tafsir_and_saadi_pages():
    scenes, total = clip()
    plays = recitation_repeats(scenes)
    by_scene = {}
    for p in plays:
        owner = next(s for s in scenes if s["at"] <= p["at"] < s["at"] + s["seconds"])
        by_scene.setdefault(owner["body"], []).append(p)
        assert p["at"] + p["seconds"] <= owner["at"] + owner["seconds"]  # never cut
        assert p["kind"] == "human" and p["repeat"]
    assert [p["file"] for p in by_scene["آية ١"]] == ["a1.mp3", "a1.mp3"]
    assert "آية ٢" not in by_scene  # 3 s left is shorter than the 4 s ayah: no cut play
    assert [p["file"] for p in by_scene["السعدي"]] == ["a1.mp3", "a2.mp3", "a1.mp3"]
    first = by_scene["آية ١"][0]
    assert first["at"] == round(scenes[1]["at"] + 0.15 + 6.0 + REPEAT_GAP, 3)
    assert all(p["at"] + p["seconds"] <= total for p in plays)


def test_nothing_added_without_recitation():
    scenes, _ = timed([scene("حديث", 30.0, role="hadith"), scene("خاتمة", 5.0, role="cta")])
    assert recitation_repeats(scenes) == []
