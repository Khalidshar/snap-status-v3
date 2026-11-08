import subprocess
from pathlib import Path

from studio_core.production import review


def fake_run(black="", silence=""):
    def run(args, timeout):
        text = black if "blackdetect" in " ".join(args) else silence
        return subprocess.CompletedProcess(args, 0, "", text)

    return run


def plan(key="news", audio=True, bodies=("أ", "ب")):
    scenes = [{"body": b} for b in bodies]
    return {"key": key, "duration": 60.0, "scenes": scenes, "audio": [{}] if audio else []}


def test_clean_clip_has_no_findings():
    assert review.review(plan(), {"cut": []}, Path("v.mp4"), fake_run()) == []


def test_reviewer_reports_cut_black_silence_and_repeats():
    run = fake_run(
        black="black_start:10 black_end:11.2 black_duration:1.2",
        silence="silence_duration: 20.5\nsilence_duration: 10",
    )
    found = review.review(plan("verse", bodies=("آية", "آية")), {"cut": [2]}, Path("v"), run)
    codes = {f["code"]: f for f in found}
    assert codes["CUT_TEXT"]["cards"] == [2] and codes["CUT_TEXT"]["fixable"]
    assert codes["BLACK_FRAMES"]["seconds"] == 1.2 and codes["BLACK_FRAMES"]["fixable"]
    assert codes["LONG_SILENCE"]["fixable"] is False
    assert codes["REPEATED_CARD"]["cards"] == [1]


def test_silence_is_only_checked_on_religious_clips():
    run = fake_run(silence="silence_duration: 50")
    assert review.review(plan("news"), {}, Path("v"), run) == []


def test_fixer_changes_only_the_design():
    design = {"dim": 0.3, "type": {"font": "body", "cards": [{}, {"size": 72, "pages": 1}]}}
    cut = [{"code": "CUT_TEXT", "cards": [1, 3], "fixable": True}]
    fixed, actions = review.fix(design, cut)
    assert fixed["type"]["cards"][1]["pages"] == 2 and fixed["type"]["cards"][3]["pages"] == 2
    assert len(actions) == 2 and fixed["fixes"] == actions
    assert design["type"]["cards"][1]["pages"] == 1  # the original decision is untouched
    again, more = review.fix(fixed, cut)
    assert again["type"]["cards"][1]["size"] == review.NORMAL_SIZE and len(more) == 2
    assert review.fix({**again, "type": {"cards": [{}, {"pages": 2, "size": 64}]}}, cut)[1] == [
        "البطاقة ٤: صفحتان بدل القص"
    ]
