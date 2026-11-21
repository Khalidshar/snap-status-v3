"""The daily posting plan (owner 21 Nov 2026): slots, Spotlight rotation, prayer windows, pause,
radar."""

import importlib.util
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from studio_core.commands import BY_KEY
from studio_core.jobs import schedule
from studio_core.production.model import NEWS

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "schedule_tick", ROOT / "scripts/ops/schedule_tick.py"
)
tick = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tick)
R = schedule.RIYADH


def at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=R)


def test_snap_has_nine_posts_and_news_never_goes_to_spotlight():
    for day in range(16, 30):
        slots = schedule.snap_slots(date(2026, 11, day))
        assert len(slots) == 9
        spotlight = [k for _, k, where in slots if where == schedule.SPOTLIGHT]
        assert len(spotlight) == 4 and len(set(spotlight)) == 4  # no repeat within a day
        assert not set(spotlight) & NEWS
        assert all(where == schedule.STORY for _, k, where in slots if k in NEWS)
        # The evening peak (8 pm onwards) carries most posts.
        assert sum(1 for c, _, _ in slots if c >= "20:00") == 5


def test_spotlight_rotates_from_day_to_day():
    one = [k for _, k, _ in schedule.snap_slots(date(2026, 11, 21))]
    two = [k for _, k, _ in schedule.snap_slots(date(2026, 11, 22))]
    assert one != two
    assert {one[4], two[4]} == {"world", "sports"}


def test_each_clip_is_made_half_an_hour_before_posting_and_only_once():
    first = ("20261121-06:45-adhkar_am", "adhkar_am")
    assert schedule.due("snap", at(2026, 11, 21, 6, 10), set()) == []
    assert schedule.due("snap", at(2026, 11, 21, 6, 15), set()) == [first]
    assert schedule.due("snap", at(2026, 11, 21, 6, 50), {first[0]}) == []
    assert schedule.due("snap", at(2026, 11, 21, 7, 20), set())[0][1] == "saudi"
    assert schedule.due("snap", at(2026, 11, 21, 3, 0), set()) == []


def test_whatsapp_follows_three_prayers_and_two_evening_clips():
    times = schedule.prayer_times(date(2026, 11, 21))
    # AlAdhan (Umm al-Qura, Riyadh) for this day.
    expected = {"fajr": "04:52", "dhuhr": "11:39", "asr": "14:44", "maghrib": "17:05"}
    for name, clock in expected.items():
        h, m = map(int, clock.split(":"))
        assert abs((times[name] - at(2026, 11, 21, h, m)).total_seconds()) <= 180, name
    assert schedule.due("status", at(2026, 11, 21, 5, 20), set()) == [
        ("20261121-fajr", "adhkar_am")
    ]
    assert schedule.due("status", at(2026, 11, 20, 5, 20), set())[0][1] == "friday"
    assert schedule.due("status", at(2026, 11, 21, 15, 10), set()) == []  # no Asr clip
    assert schedule.prayer_command("maghrib", date(2026, 11, 22)) == "fast"  # Sunday
    assert schedule.prayer_command("maghrib", date(2026, 11, 19)) == "kahf"  # Thursday
    assert schedule.due("status", at(2026, 11, 21, 20, 40), set()) == [
        ("20261121-21:00-news", "news")
    ]


def test_every_scheduled_command_exists_in_the_menu():
    kinds = {k for _, k in schedule.STATUS_FIXED} | {schedule.RADAR}
    for day in range(16, 23):
        kinds |= {k for _, k, _ in schedule.snap_slots(date(2026, 11, day))}
        kinds |= {schedule.prayer_command(p, date(2026, 11, day)) for p in schedule.PRAYER_NAMES}
    assert kinds <= set(BY_KEY)


def test_radar_cap_and_hours():
    assert schedule.radar_open(at(2026, 11, 21, 10, 0), 2)
    assert not schedule.radar_open(at(2026, 11, 21, 10, 0), 3)
    assert not schedule.radar_open(at(2026, 11, 21, 2, 0), 0)


def test_pause_flag_and_schedule_text(tmp_path):
    assert not schedule.paused(tmp_path)
    schedule.set_paused(tmp_path, True)
    assert "⏸ موقوفة" in schedule.schedule_text("snap", at(2026, 11, 21, 9, 0), tmp_path)
    schedule.set_paused(tmp_path, False)
    snap = schedule.schedule_text("snap", at(2026, 11, 21, 9, 0), tmp_path)
    assert "(الأضواء)" in snap and "(القصص)" in snap and "حتى ٣" in snap
    text = schedule.schedule_text("status", at(2026, 11, 21, 9, 0), tmp_path)
    assert "▶️ شغالة" in text and "بعد الفجر" in text and "بعد العصر" not in text


class Recorder:
    def __init__(self, results):
        self.calls, self.results = [], results

    def __call__(self, app, command):
        self.calls.append((app, command))
        return {"app": app, "command": command, "result": self.results.get(command, "SEND_DONE")}


def folders(tmp_path, monkeypatch):
    monkeypatch.setattr(tick, "STATE", tmp_path)
    for app in ("snap", "status"):
        (tmp_path / app).mkdir()


def test_tick_runs_due_slots_once_and_respects_pause_and_halt(tmp_path, monkeypatch):
    folders(tmp_path, monkeypatch)
    rec = Recorder({"urgent": "PRODUCE_FAILED"})
    monkeypatch.setattr(tick, "run", rec)
    now = at(2026, 11, 21, 6, 20)
    tick.tick(now)
    assert ("snap", "adhkar_am") in rec.calls and ("snap", "urgent") in rec.calls
    rec.calls.clear()
    tick.tick(now)
    assert rec.calls == [("snap", "urgent")]  # slot done; the radar keeps looking
    schedule.set_paused(tmp_path / "snap", True)
    (tmp_path / "status" / "HALT").write_text("")
    rec.calls.clear()
    tick.tick(at(2026, 11, 21, 7, 10))
    assert rec.calls == []


def test_radar_counts_only_sent_clips_up_to_the_cap(tmp_path, monkeypatch):
    folders(tmp_path, monkeypatch)
    monkeypatch.setattr(tick, "run", Recorder({}))
    for minute in range(0, 120, 10):
        tick.tick(at(2026, 11, 21, 14, 0) + timedelta(minutes=minute))
    saved = json.loads((tmp_path / "snap/schedule-done.json").read_text())
    assert saved["radar"]["20261121"] == 3
