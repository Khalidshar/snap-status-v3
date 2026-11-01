"""Reviewed riddles: timed reveal, pixel bounds, owned no-repeat and real media."""

import hashlib
import json
import shutil
import subprocess
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from snap_bot import catalogs, riddle_card
from snap_bot.arabic_agent import inspect_content
from snap_bot.content_agents import CHECKS, run_check
from snap_bot.did_you_know import (
    WINDOW_SECONDS,
    FactsExhausted,
    choose_fact,
    load_kind,
    make_content,
    reservation_state,
    seed_history,
    validate_content,
)
from snap_bot.fact_layout import display_text, label_y, measure_label
from snap_bot.font_admission import font_paths
from studio_core.config import Config
from studio_core.jobs import Repository

ROOT = Path(__file__).resolve().parents[3]


def values():
    return [make_content(f, time.time(), "riddle") for f in load_kind("riddle")]


def test_all_catalog_pairs_are_unchanged_and_arabic_checked():
    for item, value in zip(catalogs.load("riddle"), values(), strict=True):
        assert value["body"] == item["question"] + "\n" + item["answer"]
        assert validate_content(value)["id"] == item["id"]
        assert inspect_content(value)["decision"] == "PASS"
        with pytest.raises(ValueError):
            inspect_content({**value, "body": value["body"] + " جواب"})


@pytest.mark.parametrize("duration", [8, 12])
def test_five_seconds_of_countdown_then_answer_only(duration):
    for value in values():
        labels, windows = riddle_card.layout(value, duration)
        digits = [
            (row[0], window)
            for row, window in zip(labels, windows, strict=True)
            if row[0] in "٥٤٣٢١"
        ]
        assert digits == [(digit, (float(i), float(i + 1))) for i, digit in enumerate("٥٤٣٢١", 1)]

        def visible(t):
            return [
                row[0]
                for row, (start, end) in zip(labels, windows, strict=True)
                if start <= t < end
            ]

        question, answer = value["body"].split("\n")
        for t in [0.5, 1.5, 5.999]:
            assert "الإجابة" not in visible(t)
            assert " ".join(visible(t)[3:]).startswith(question)
        assert "الإجابة" in visible(6)
        assert " ".join(visible(6)[3:]).startswith(answer)
        assert not any(digit in visible(6) for digit in "٥٤٣٢١")
        assert [row[0] for row in labels].count("روّق مع خالد") == 1
        assert value["attribution"] not in [row[0] for row in labels]
    for bad in [7, 13]:
        with pytest.raises(ValueError):
            riddle_card.layout(value, bad)


def test_reserved_riddles_pass_agents_without_repeating(tmp_path):
    repo = Repository(
        replace(
            Config.load(ROOT / "config/snap/did_you_know.toml"),
            data_root=tmp_path,
            max_running=100,
            rate_limit=100,
            max_active_per_actor=100,
        )
    )
    seen = set()
    for i in range(31):
        job, _ = repo.enqueue(
            str(i), "synthetic", "did_you_know", "NORMAL", "input", time.time() + 600
        )
        claim = repo.claim(job_id=job["id"])
        args = claim["id"], claim["lease_token"], claim["attempt"]
        if i == 30:
            with pytest.raises(FactsExhausted):
                choose_fact(repo, *args, kind="riddle")
            break
        value = choose_fact(repo, *args, kind="riddle")[0]
        assert value["fact_id"] not in seen
        seen.add(value["fact_id"])
        for role in CHECKS:
            assert run_check(role, repo, *args, value) == value
        assert inspect_content(value)["decision"] == "PASS"
    assert len(seen) == 30


def test_confirmed_history_expires_at_thirty_days(tmp_path):
    repo = Repository(
        replace(Config.load(ROOT / "config/snap/did_you_know.toml"), data_root=tmp_path)
    )
    value = values()[0]
    at = time.time()
    with repo.transaction() as db:
        db.execute(
            "CREATE TABLE fact_reservations(job_id TEXT, fact_id TEXT, body_hash TEXT, reserved_at REAL, content_json TEXT)"
        )
        seed_history(
            db,
            [
                {
                    "fact_id": value["fact_id"],
                    "body_hash": hashlib.sha256(value["body"].encode()).hexdigest(),
                    "category": "riddle",
                    "kind": "riddle",
                    "at": at,
                    "until": at + WINDOW_SECONDS,
                }
            ],
        )
        assert ("id", value["fact_id"]) in reservation_state(
            db, at + WINDOW_SECONDS - 1, kind="riddle"
        )[0]
        assert ("id", value["fact_id"]) not in reservation_state(
            db, at + WINDOW_SECONDS, kind="riddle"
        )[0]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="Linux FFmpeg pixel measurement")
def test_every_riddle_line_fits_safe_in_pixels(tmp_path):
    profile = tomllib.loads((ROOT / "config/snap/did_you_know.toml").read_text())["slice"][
        "profile"
    ]
    font, latin = font_paths(profile)
    for value in values():
        for i, row in enumerate(riddle_card.layout(value, 8)[0]):
            text, size, _, _, role = row
            path = tmp_path / f"{value['fact_id']}-{i}.txt"
            path.write_text(display_text(text, role))
            selected = latin if role == "latin" else font
            measure_label(
                profile,
                f"drawtext=fontfile={selected}:textfile={path}:expansion=none:text_shaping=1:fontsize={size}:fontcolor=white:x=(w-text_w)/2:y={label_y(row)}",
            )


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="Real Linux video")
def test_real_riddle_video_and_media_agents():
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
        fixture.set_content(values()[0])
        profile = fixtures.fact_profile()
        assert profile is not None
        f.settings["profile"] = profile
        with patch("snap_bot.render.proof", return_value=ownership.ISOLATION):
            result = render(SimpleNamespace(app="snap-bot-v2"), f.settings, f.job, f.digest, 1)
        assert result["qa"]["duration"] == 8
        assert check_media(result, f.settings)["decision"] == "PASS"
        assert check_voice(result)["decision"] == "PASS"
        video = Path(f.settings["spool_root"]).parent / result["artifact"]
        frames = []
        for second in [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5]:
            raw = subprocess.run(
                [
                    profile["ffmpeg"],
                    "-v",
                    "error",
                    "-ss",
                    str(second),
                    "-i",
                    str(video),
                    "-vf",
                    "crop=820:720:130:600,format=gray",
                    "-frames:v",
                    "1",
                    "-threads",
                    "1",
                    "-f",
                    "rawvideo",
                    "-",
                ],
                capture_output=True,
                check=True,
                timeout=30,
            ).stdout
            assert len(raw) == 820 * 720
            frames.append(hashlib.sha256(raw).hexdigest())
        assert len(set(frames)) == 7
        script = next(video.parents[1].glob("design/*/filters.txt")).read_text()
        assert "gte(t,6)*lt(t,8)" in script
        f.rendering()
        f.adopt(result)
    finally:
        fixture.doCleanups()


def test_arabic_contract_evidence_replays_the_riddle_report():
    from snap_bot.owned_contract_adapter import (
        ContractViolation,
        arabic_evidence,
        contract_plan,
        digest,
    )

    value = values()[0]
    sha = digest(value)
    saved = {
        "kind": "riddle",
        "fact_id": value["fact_id"],
        "body": value["body"],
        "content_hash": value["content_hash"],
        "content_sha": sha,
        "report": inspect_content(value),
    }
    row = {
        "plan_sha": contract_plan()["sha256"],
        "status": "SUCCEEDED",
        "input_sha": sha,
        "output_sha": sha,
        "output_json": json.dumps(saved),
    }
    arabic_evidence(row, sha, value["content_hash"])
    saved["report"]["line_lengths"] = []
    with pytest.raises(ContractViolation):
        arabic_evidence({**row, "output_json": json.dumps(saved)}, sha, value["content_hash"])
