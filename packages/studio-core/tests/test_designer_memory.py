from studio_core.production import designer


def test_designer_rotates_three_clips_per_bot_and_pins_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(designer, "luma", lambda _: 80.0)
    plan = {"key": "facts", "scenes": [{"body": "مقطع نصي"}]}
    clips = [{"file": f"clip-{i}.mp4", "title": "خلفية", "pool": "day"} for i in range(4)]

    def pick(bot, job, pool=clips):
        return designer.decide(plan, {}, pool, history=(tmp_path / f"{bot}.sqlite", job))

    snap = [pick("snap", str(i)) for i in range(4)]
    assert len({result["footage"] for result in snap}) == 4
    assert "آخر ثلاثة مقاطع" in " ".join(snap[1]["reasons"])
    assert pick("snap", "3")["footage"] == snap[3]["footage"]
    assert pick("status", "0")["footage"] == snap[0]["footage"]
    assert pick("snap", "4", clips[:1])["footage"] == clips[0]["file"]
