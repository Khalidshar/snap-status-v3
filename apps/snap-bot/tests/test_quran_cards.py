"""Verbatim mixed-faith pages, pinned fonts, measured cards and media admission."""

import copy
import hashlib
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

import pytest

from snap_bot import faith, quran_font, sacred_cards, sacred_text
from snap_bot.did_you_know import load_kind, make_content
from snap_bot.fact_layout import page_display_body, sacred_labels, stack_sacred
from snap_bot.render import sacred_sequence

ROOT = Path(__file__).resolve().parents[3]


def content(kind, identifier):
    fact = next(f for f in load_kind(kind) if f["id"] == identifier)
    return make_content(fact, time.time(), kind)


@pytest.mark.parametrize("kind", sorted(sacred_cards.KINDS))
def test_every_new_item_preserves_exact_source_hash_and_font(kind):
    for item in faith.load(kind):
        value = content(kind, item["id"])
        text = faith.text(item)
        assert value["body"] == text
        for limit in (80, 120, 180):
            parts = sacred_text.pages(text, limit)
            assert " ".join(parts).encode() == text.encode()
            assert (
                hashlib.sha256(" ".join(parts).encode()).hexdigest()
                == hashlib.sha256(text.encode()).hexdigest()
            )
        selected = quran_font.font_for(item)
        assert selected == (
            quran_font.path()
            if kind == "verse" or item.get("type") == "quran"
            else quran_font.NASKH
        )
        displayed = page_display_body(value, text)
        assert displayed == ("\ufd3f" + text + "\ufd3e" if selected == quran_font.path() else text)


def test_verse_reference_and_repetition_labels_are_outside_locked_body():
    value = content("verse", "verse-01")
    labels = [r[0] for r in sacred_labels(value)]
    assert "قال الله تعالى:" in labels
    assert any("البقرة" in row and "١٥٢" in row for row in labels)
    value = content("adhkar_am", "adhkar-am-02")
    labels = [r[0] for r in sacred_labels(value)]
    assert "أذكار الصباح" in labels and "يقرأ ٣ مرات" in labels
    assert "يقرأ" not in value["body"]


def test_media_rechecks_every_page_font_hash_and_duration():
    value = content("adhkar_am", "adhkar-am-01")
    parts = sacred_text.pages(value["body"], 160)
    result = {
        "content_hash": value["content_hash"],
        "sacred_pagination": sacred_cards.evidence(value, parts, 8),
    }
    assert sacred_cards.duration(result, 8) == 8 * len(parts)
    for changed in ("page", "font", "time", "hash", "order"):
        bad = copy.deepcopy(result)
        record = bad["sacred_pagination"]
        if changed == "page":
            record["pages"][0] = record["pages"][0].replace("َ", "", 1)
        if changed == "font":
            record["font_sha256"] = quran_font.NASKH_SHA256
        if changed == "time":
            record["page_seconds"] = 12
        if changed == "hash":
            bad["content_hash"] = "0" * 64
        if changed == "order":
            record["pages"].reverse()
        with pytest.raises(ValueError):
            sacred_cards.duration(bad, 8)


@pytest.mark.parametrize("kind", sorted(sacred_cards.KINDS))
def test_new_kind_passes_every_content_agent_verbatim(tmp_path, kind):
    from dataclasses import replace

    from snap_bot.content_agents import CHECKS, run_check
    from snap_bot.did_you_know import choose_fact
    from studio_core.config import Config
    from studio_core.jobs import Repository

    repo = Repository(
        replace(Config.load(ROOT / "config/snap/did_you_know.toml"), data_root=tmp_path)
    )
    job, _ = repo.enqueue(
        "sacred-" + kind, "synthetic", "did_you_know", "NORMAL", "input", time.time() + 600
    )
    claim = repo.claim(job_id=job["id"])
    value = choose_fact(repo, claim["id"], claim["lease_token"], claim["attempt"], kind=kind)[0]
    for role in CHECKS:
        assert (
            run_check(role, repo, claim["id"], claim["lease_token"], claim["attempt"], value)
            == value
        )


@pytest.mark.skipif(not shutil.which("pango-view"), reason="Linux Pango pixel measurement")
@pytest.mark.parametrize("kind", sorted(sacred_cards.KINDS))
def test_all_quran_and_adhkar_pages_are_measured_inside_card(tmp_path, kind):
    profile = tomllib.loads((ROOT / "config/snap/did_you_know.toml").read_text())["slice"][
        "profile"
    ]
    profile["pango_view"] = shutil.which("pango-view")
    for item in faith.load(kind):
        directory = tmp_path / item["id"]
        directory.mkdir()
        value = content(kind, item["id"])
        parts, images = sacred_cards.images(value, profile, directory)
        assert " ".join(parts) == faith.text(item)
        height = max(size[1] for _, size in images)
        labels, top = stack_sacred(sacred_labels(value, profile), height)
        for image, (width, measured_height) in images:
            assert image.is_file() and 0 < width <= 820 and 0 < measured_height <= 720
            assert top + measured_height < labels[-2][2]
        if item["id"] == "adhkar-am-01":
            assert len(parts) > 1  # آية الكرسي cannot be cropped into one card.


@pytest.mark.skipif(not shutil.which("pango-view"), reason="Linux Pango and FFmpeg")
def test_real_ffmpeg_shows_each_page_in_its_own_interval(tmp_path):
    profile = tomllib.loads((ROOT / "config/snap/did_you_know.toml").read_text())["slice"][
        "profile"
    ]
    profile["pango_view"] = shutil.which("pango-view")
    value = content("adhkar_am", "adhkar-am-01")
    parts, images = sacred_cards.images(value, profile, tmp_path)
    height = max(size[1] for _, size in images)
    _, top = stack_sacred(sacred_labels(value, profile), height)
    boxes = []
    script = sacred_sequence(images, top, height, 1, {"fps": 24}, "null", (0, 0.1), boxes)
    graph = tmp_path / "filters.txt"
    graph.write_text(script.replace("[in]", "[0:v]", 1))
    hashes = []
    for index in range(len(parts)):
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-filter_complex_threads",
            "1",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=1080x1920:r=24:d={len(parts)}",
            "-filter_complex_script",
            str(graph),
            "-map",
            "[out]",
            "-ss",
            str(index + 0.5),
            "-frames:v",
            "1",
            "-threads",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ]
        raw = subprocess.run(command, capture_output=True, check=True, timeout=30).stdout
        assert len(raw) == 1080 * 1920 and max(raw) > 100
        hashes.append(hashlib.sha256(raw).hexdigest())
    assert len(set(hashes)) == len(parts)
    assert len(boxes) == len(parts)


@pytest.mark.skipif(not shutil.which("pango-view"), reason="Linux Pango and FFmpeg")
def test_complete_paginated_video_passes_media_and_voice_agents():
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import patch

    import test_did_you_know_render as fixtures
    import test_render_ownership as ownership

    from snap_bot.render import render
    from snap_bot.render_agents import check_media, check_voice

    fixture = fixtures.FactRendererTests()
    fixture.setUp()
    try:
        f = fixture.fixture
        f.config = replace(f.config, lease_seconds=600)
        f.repo.config = f.config
        f.repo.heartbeat(f.job, f.owner["lease_token"])
        value = content("adhkar_am", "adhkar-am-01")
        fixture.set_content(value)
        profile = tomllib.loads((ROOT / "config/snap/did_you_know.toml").read_text())["slice"][
            "profile"
        ]
        profile["pango_view"] = shutil.which("pango-view")
        profile["pango_version"] = subprocess.run(
            [profile["pango_view"], "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
        profile["ffmpeg_version"] = subprocess.run(
            [profile["ffmpeg"], "-version"], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0]
        f.settings["profile"] = profile
        with patch("snap_bot.render.proof", return_value=ownership.ISOLATION):
            result = render(
                SimpleNamespace(app="snap-bot-v2"), f.settings, f.job, value["content_hash"], 1
            )
        pages = result["sacred_pagination"]["pages"]
        assert len(pages) > 1 and " ".join(pages) == value["body"]
        assert result["qa"]["duration"] == len(pages) * profile["duration_seconds"]
        assert check_media(result, f.settings)["decision"] == "PASS"
        assert check_voice(result)["decision"] == "PASS"
        f.rendering()
        f.adopt(result)
    finally:
        fixture.doCleanups()
