"""Scheduled slots (owner decision 9 Nov 2026): three clips a day, Friday Kahf, HALT respected."""

import importlib.util
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "schedule_tick", ROOT / "scripts/ops/schedule_tick.py"
)
tick = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tick)


def test_morning_is_adhkar_and_friday_is_kahf():
    assert tick.command_for("morning", date(2026, 11, 9)) == "adhkar_am"  # Monday
    assert tick.command_for("morning", date(2026, 11, 13)) == "kahf"  # Friday


def test_noon_rotates_through_every_general_kind():
    seen = {tick.command_for("noon", date(2026, 11, d)) for d in range(1, 9)}
    assert seen == set(tick.NOON)


def test_night_alternates_isha_and_evening_adhkar():
    days = [tick.command_for("night", date(2026, 11, d)) for d in (9, 10)]
    assert sorted(days) == ["adhkar_pm", "isha"]


def test_halt_skips_the_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(tick, "STATE", tmp_path)
    (tmp_path / "snap").mkdir()
    (tmp_path / "snap" / "HALT").write_text("")
    assert tick.run("snap", "facts")["result"] == "SKIPPED_HALT"


def test_every_scheduled_command_exists():
    from studio_core.commands import BY_KEY

    for kind in (*tick.NOON, "adhkar_am", "kahf", "isha", "adhkar_pm"):
        assert kind in BY_KEY
