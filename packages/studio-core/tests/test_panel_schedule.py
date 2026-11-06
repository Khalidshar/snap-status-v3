import json

import pytest

from studio_core.jobs import panel_schedule


def test_snap_reads_slots_and_pause_without_executing_script(tmp_path):
    script = tmp_path / "snap.py"
    script.write_text(
        "raise RuntimeError('must not execute')\n"
        "SLOTS = [('06:30', 'adhkar_am')]\n"
        "RADAR_CAP = {'urgent': 3}\n"
    )
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"paused": False, "halted": True}))
    slots = [["adhkar_am", "06:30"], ["urgent", "radar"]]
    assert panel_schedule.read("snap", script, state) == {"paused": True, "slots": slots}
    for value in ("{", "[]", '{"paused": 1}'):
        state.write_text(value)
        assert panel_schedule.read("snap", script, state) == {"paused": None, "slots": slots}
    assert panel_schedule.read("snap", script, tmp_path / "missing.json")["paused"] is None


def test_status_reads_fixed_times_and_relative_prayer_slots(tmp_path):
    script = tmp_path / "run.py"
    script.write_text(
        "NEWS_AT = (12, 5)\n"
        "BEFORE, AFTER = (0, 15)\n"
        "def due():\n"
        "    for item in ['unused']:\n"
        "        pass\n"
        "    for key, clock in {'news': NEWS_AT, 'night': (22, 5)}.items():\n"
        "        pass\n"
    )
    state = tmp_path / "state.json"
    state.write_text("{}")
    assert panel_schedule.read("status", script, state) == {
        "paused": False,
        "slots": [
            ["news", "12:05"],
            ["night", "22:05"],
            ["adhkar_am", "fajr+15"],
            ["friday", "fajr+15"],
            ["dua", "dhuhr+15"],
            ["adhkar_pm", "asr+15"],
            ["maghrib", "maghrib+15"],
            ["fast", "maghrib+15"],
            ["kahf", "maghrib+15"],
            ["isha", "isha+15"],
            ["urgent", "radar"],
        ],
    }


@pytest.mark.parametrize(
    "source",
    [
        "AFTER = 15\n",
        "def due():\n    pass\n",
        "def due():\n    for key in {**OTHER}.items():\n        pass\n",
    ],
)
def test_status_rejects_missing_or_dynamic_schedule(tmp_path, source):
    script = tmp_path / "run.py"
    script.write_text(source)
    with pytest.raises(ValueError, match="LEGACY_SCHEDULE_UNAVAILABLE"):
        panel_schedule.read("status", script, None)
    with pytest.raises(ValueError, match="INVALID_LEGACY_SCRIPT"):
        panel_schedule.read("snap", script, None)


@pytest.mark.parametrize(
    "schedule",
    [
        [],
        {"other": {}},
        {"snap": None},
        {"snap": {"paused": "yes", "slots": [["news", "08:00"]]}},
        {"snap": {"paused": False, "slots": []}},
        {"snap": {"paused": False, "slots": ["news"]}},
        {"snap": {"paused": False, "slots": [[1, "08:00"]]}},
        {"snap": {"paused": False, "slots": [["News", "08:00"]]}},
        {"snap": {"paused": False, "slots": [["news", 800]]}},
        {"snap": {"paused": False, "slots": [["news", "25:00"]]}},
    ],
)
def test_rejects_malformed_schedule(schedule):
    with pytest.raises(ValueError, match="INVALID_SCHEDULE"):
        panel_schedule.validate(schedule)
