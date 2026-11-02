"""Licensed backdrops inside the approved frame (owner decision 2026-11-01)."""

import json
import shutil
from pathlib import Path

import pytest

from snap_bot import footage


def test_every_clip_is_pinned_licensed_and_sorted_into_a_pool():
    clips = footage.load()
    assert len(clips) >= 8
    assert {c["pool"] for c in clips} == set(footage.POOLS)
    for clip in clips:
        assert clip["provider"] == "Mixkit"
        assert clip["license"] == "Mixkit Stock Video Free License"
        assert clip["source"].startswith("https://mixkit.co/free-stock-video/")
        assert 0 < (footage.ROOT / clip["file"]).stat().st_size <= 10_000_000


def test_sacred_kinds_get_calm_backdrops_and_a_retry_keeps_the_same_one():
    for kind in ("hadith", "dua", "fast", "verse", "adhkar_am", "adhkar_pm", "friday", "kahf"):
        first = footage.choose({"kind": kind, "body": "نص ثابت"})
        assert first["pool"] == "calm"
        assert footage.choose({"kind": kind, "body": "نص ثابت"}) == first
    assert footage.choose({"kind": "night", "body": "x"})["pool"] == "night"
    assert footage.choose({"kind": "riddle", "body": "x"})["pool"] == "day"


def test_a_changed_or_relicensed_clip_is_refused(tmp_path: Path):
    root = tmp_path / "footage"
    shutil.copytree(footage.ROOT, root)
    clip = footage.load(root)[0]
    (root / clip["file"]).write_bytes(b"not the pinned clip")
    with pytest.raises(ValueError, match="fingerprint or license"):
        footage.load(root)
    shutil.rmtree(root)
    shutil.copytree(footage.ROOT, root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["clips"][0]["license"] = "unknown"
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint or license"):
        footage.load(root)


def test_filters_fill_the_canvas_and_dim_for_text():
    chain = footage.filters(24)
    assert chain[:3] == [
        "scale=1080:1920:force_original_aspect_ratio=increase",
        "crop=1080:1920",
        "fps=24",
    ]
    assert any("black@0.50" in f for f in chain)
    with pytest.raises(ValueError):
        footage.filters(0)
