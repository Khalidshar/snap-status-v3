import json
import subprocess
from pathlib import Path

import pytest

from studio_core.production import render, review


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


@pytest.mark.parametrize("persistent_black", [False, True])
def test_render_retries_review_findings_once(tmp_path, monkeypatch, persistent_black):
    source = tmp_path / "source"
    source.mkdir()
    content = {"content_hash": "a" * 64}
    original = json.dumps(content)
    (source / "content.json").write_text(original)
    clip_plan = {**plan(), "app": "snap-bot-v2"}
    design = {"style": "solid", "dim": 0.3, "footage_path": "background.mp4"}
    applied = []
    mixes = []

    monkeypatch.setattr(render, "validate", lambda *_args, **_kwargs: clip_plan)
    monkeypatch.setattr(render, "verify_fonts", lambda _: None)
    monkeypatch.setattr(render, "deny_ip_sockets", lambda: None)
    monkeypatch.setattr(render, "proof", lambda _: {})
    monkeypatch.setattr(render.footage, "load", list)
    monkeypatch.setattr(render.designer, "decide", lambda *_args, **_kwargs: design)
    monkeypatch.setattr(render.designer, "inspect", lambda *_args: {})
    monkeypatch.setattr(
        render.brand_frame,
        "load_brand",
        lambda: {"clip_box": [], "overlay": {"sha256": "b" * 64}},
    )
    monkeypatch.setattr(
        render.brand_frame,
        "command",
        lambda ffmpeg, clip, video, *args: [
            ffmpeg,
            "-preset",
            "stub",
            "-crf",
            "0",
            "-maxrate",
            "0",
            "-bufsize",
            "0",
            str(video),
        ],
    )

    def mix(plan, source, work):
        mixes.append(plan["audio"])
        return work / "mix.wav"

    def qa(video, plan, layout, work):
        for i in range(1, 7):
            (work / f"shot-{i}.jpg").write_bytes(b"frame")
        return {"result": "PASS"}

    def run(args, timeout):
        if args[1].endswith("render_legacy_scenes.cjs"):
            frames = Path(args[4])
            frames.mkdir()
            applied.append(json.loads(Path(args[5]).read_bytes()))
            (frames / "layout.json").write_text(
                json.dumps({"cut": [0] if len(applied) == 1 else []})
            )
        elif "blackdetect=d=0.5:pix_th=0.06" in args:
            black = persistent_black or len(applied) == 1
            return fake_run(black="black_duration:1.0" if black else "")(args, timeout)
        else:
            Path(args[-1]).write_bytes(b"rendered video")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(render, "run", run)
    monkeypatch.setattr(render, "audio_mix", mix)
    monkeypatch.setattr(render, "qa", qa)
    out = tmp_path / "out"
    if persistent_black:
        with pytest.raises(ValueError, match="LEGACY_REVIEW_BLACK_FRAMES"):
            render.render(source, out)
        assert not (out / "video.mp4").exists()
    else:
        result = render.render(source, out)
        assert result["attempt"] == 2
        assert result["review"]["findings"] == []
        assert len(result["review"]["fixes"]) == 2
    assert len(applied) == 2
    assert "type" not in applied[0] and applied[1]["type"]["cards"][0]["pages"] == 2
    assert mixes == [clip_plan["audio"], clip_plan["audio"]]
    assert (source / "content.json").read_text() == original
