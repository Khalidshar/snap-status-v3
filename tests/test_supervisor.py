"""The supervisor runs against a fake host: no systemd, journal, network or credentials."""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[1] / "scripts/ops"
NOW = 1_795_000_000.0  # a fixed clock (Riyadh 18 Nov, 14:06)


def load():
    spec = importlib.util.spec_from_file_location("supervisor", OPS / "supervisor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sv = load()


class Host(sv.Probe):
    def __init__(self, state: Path):
        super().__init__(state)
        self.down: set[str] = set()
        self.failed: list[tuple[str, str]] = []
        self.rows: list[dict] = []
        self.restarted: list[str] = []
        self.restart_works = True
        self.renders: list[tuple[str, float]] = []
        self.disk_used = 0.2
        self.mem = 4000

    def active(self, unit):
        return "failed" if unit in self.down else "active"

    def failed_units(self):
        return self.failed

    def running_since(self, pattern):
        return self.renders

    def journal(self, cursor, since):
        rows, self.rows = self.rows, []
        return rows, ("c" + str(len(rows))) if rows else cursor

    def disk(self):
        return self.disk_used

    def memory_mb(self):
        return self.mem

    def restart(self, unit):
        self.restarted.append(unit)
        if self.restart_works:
            self.down.discard(unit)
        return self.restart_works


def line(message: str, at: float = NOW) -> dict:
    return {"at": at, "unit": "x", "message": message}


def args(home: Path, state: Path, now: float = NOW, **extra):
    values = {
        "home": str(home),
        "state": str(state),
        "dry_run": False,
        "print": False,
        "no_restart": False,
        "report": False,
        "since_hours": "1",
        "now": str(now),
    }
    values.update(extra)
    return type("A", (), values)()


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    (state / "snap").mkdir(parents=True)
    (state / "status/commands").mkdir(parents=True)
    sent: list[str] = []
    monkeypatch.setattr(sv, "deliver", lambda messages, dry: sent.extend(messages))
    return tmp_path / "home", state, Host(state), sent


def test_a_core_service_down_is_restarted_once_and_reported(env):
    home, state, host, sent = env
    host.down.add("madar-v3-snap-commands.service")
    sv.run(args(home, state), host)
    assert host.restarted == ["madar-v3-snap-commands.service"]
    assert "خدمة واقفة: بوت تيليجرام (سناب)" in sent[0] and "أعدت تشغيلها ✅" in sent[0]
    # Back up: the incident resolves with its duration.
    sent.clear()
    sv.run(args(home, state, NOW + 60), host)
    assert "✅ انحل: خدمة واقفة: بوت تيليجرام (سناب)" in sent[0]
    # Down again within the hour: reported, never restarted again.
    sent.clear()
    host.down.add("madar-v3-snap-commands.service")
    sv.run(args(home, state, NOW + 600), host)
    assert host.restarted == ["madar-v3-snap-commands.service"]
    assert "ما أعيد تشغيلها" in sent[0]


def test_dry_run_and_no_restart_never_touch_a_service(env):
    home, state, host, sent = env
    host.down.add("madar-v3-panel.service")
    messages = sv.run(args(home, state, dry_run=True), host)
    assert host.restarted == [] and sent == []
    assert "تجربة" in messages[0]


def test_a_failed_restart_is_said_plainly(env):
    home, state, host, sent = env
    host.restart_works = False
    host.down.add("madar-v3-status-commands.service")
    sv.run(args(home, state), host)
    assert "وما رجعت ❌" in sent[0]


def test_blocked_snap_send_alerts_once_with_a_repair_request(env):
    home, state, host, sent = env
    events = state / "snap/events.jsonl"
    events.write_text(
        json.dumps({"seq": 1, "kind": "INTENT", "time": NOW - 30, "video_sha": "a" * 64})
        + "\n"
        + json.dumps(
            {"seq": 2, "kind": "PRE_NETWORK_BLOCKED", "time": NOW - 20, "video_sha": "a" * 64}
        )
        + "\n"
    )
    sv.run(args(home, state), host)
    assert "🚨 إرسال سناب انمنع" in sent[0] and "📝" in sent[0]
    requests = list((home / "requests").glob("*.md"))
    assert len(requests) == 1 and "طلب إصلاح: إرسال سناب انمنع" in requests[0].read_text()
    sent.clear()
    sv.run(args(home, state, NOW + 60), host)
    assert sent == []


def test_first_run_ignores_old_history(env):
    home, state, host, sent = env
    (state / "snap/events.jsonl").write_text(
        json.dumps({"seq": 9, "kind": "PRE_NETWORK_BLOCKED", "time": NOW - 86400 * 3}) + "\n"
    )
    sv.run(args(home, state), host)
    assert sent == []


def test_repeated_telegram_crashes_merge_into_one_incident(env):
    home, state, host, sent = env
    for n in range(3):
        host.rows = [
            {"at": NOW + n, "unit": "x", "message": "OWNER_COMMANDS_BLOCKED: TimeoutError"}
        ]
        sv.run(args(home, state, NOW + n * 60), host)
    text = "\n".join(sent)
    assert text.count("🚨 بوت تيليجرام طاح ورجع") == 1
    assert "تكرر ٣ مرات" in text


def test_no_fresh_story_and_send_stage_are_not_double_reported(env):
    home, state, host, sent = env
    host.rows = [
        line("SNAP_PREVIEW_FAILURE stage=render code=NO_FRESH_CONTENT"),
        line("SNAP_PREVIEW_FAILURE stage=send code=UNEXPECTED_FAILURE"),
    ]
    host.failed = [("madar-v3-snap-send-main@abc.service", "Tue")]
    sv.run(args(home, state), host)
    assert sent == []


def test_a_render_failure_is_reported(env):
    home, state, host, sent = env
    host.rows = [
        {"at": NOW, "unit": "x", "message": "SNAP_PREVIEW_FAILURE stage=render code=QA_FAILED"}
    ]
    sv.run(args(home, state), host)
    assert "فشل صنع مقطع سناب — المرحلة render — QA_FAILED" in sent[0]


def test_whatsapp_queue_failures_and_stuck_clips(env):
    home, state, host, sent = env
    db = sqlite3.connect(state / "status/commands/jobs.sqlite")
    db.execute("CREATE TABLE command_videos(tag, kind, state, at, code)")
    db.execute(
        "INSERT INTO command_videos VALUES('t1','saudi','BLOCKED_OR_UNKNOWN',?, 'X_FAILED')",
        (NOW - 10,),
    )
    db.execute(
        "INSERT INTO command_videos VALUES('t2','style','BLOCKED',?, 'NO_FRESH_FASHION')",
        (NOW - 10,),
    )
    db.execute("INSERT INTO command_videos VALUES('t3','world','WORKING',?, '')", (NOW - 3600,))
    db.commit()
    sv.run(args(home, state), host)
    text = "\n".join(sent)
    assert "مقطع واتساب ما انرسل — saudi" in text
    assert "style" not in text  # no fresh fashion story is not a fault
    assert "مقطع واتساب متعلّق — world" in text


def test_stuck_render_disk_memory_and_sources(env):
    home, state, host, sent = env
    host.renders = [("madar-v3-snap-render@saudi.service", NOW - 3600)]
    host.disk_used, host.mem = 0.9, 200
    host.rows = [{"at": NOW - 10, "unit": "x", "message": "PRODUCTION_SOURCE_HOST_DENIED"}] * 45
    sv.run(args(home, state), host)
    text = "\n".join(sent)
    assert "صنع مقطع متعلّق" in text and "القرص قرب يمتلي" in text and "الذاكرة قليلة" in text
    assert "مصادر ترفض" not in text  # info only: in the ledger and the report, not a ping
    rows = (
        sqlite3.connect(home / "incidents.sqlite").execute("SELECT title FROM incidents").fetchall()
    )
    assert ("مصادر ترفض كثير",) in rows


def test_long_open_faults_get_one_reminder(env):
    home, state, host, sent = env
    host.restart_works = False
    host.down.add("madar-v3-panel.service")
    sv.run(args(home, state), host)
    sent.clear()
    sv.run(args(home, state, NOW + 3600), host)
    assert sent == []
    sv.run(args(home, state, NOW + 7 * 3600), host)
    assert "⏰ لسا قائم: خدمة واقفة: لوحة التحكم" in sent[0]


def test_secrets_never_reach_a_message_or_request():
    text = sv.clean("fail 1234567:" + "A" * 35 + " Bearer abc token=xyz")
    assert "AAAA" not in text and "abc" not in text and "xyz" not in text


def test_many_lines_are_batched_into_two_messages():
    messages = sv.compose([f"🚨 عطل {n}" for n in range(30)])
    assert len(messages) == 2 and "غيرها في السجل" in messages[-1]
    assert sv.compose(["🚨 نفس", "🚨 نفس"]) == ["🛡️ مشرف روّق\n🚨 نفس (٢ مرات)"]


def test_morning_report_once_a_day(env):
    home, state, host, sent = env
    host.down.add("madar-v3-panel.service")
    sv.run(args(home, state), host)
    sent.clear()
    morning = 1_795_064_400.0  # 08:00 Riyadh the next day
    sv.run(args(home, state, morning), host)
    report = sent[-1]
    assert report.startswith("🛡️ تقرير المشرف") and "خدمة واقفة: لوحة التحكم" in report
    assert "إعادة تشغيل: ١" in report
    sent.clear()
    sv.run(args(home, state, morning + 60), host)
    assert not any(m.startswith("🛡️ تقرير المشرف") for m in sent)


def test_watchdog_speaks_only_when_the_supervisor_is_silent(env):
    home, state, host, _ = env
    sv.run(args(home, state), host)
    assert sv.watchdog(args(home, state, NOW + 300)) == []
    message = sv.watchdog(args(home, state, NOW + 3600))
    assert message and "وقف عن الفحص" in message[0]


def test_events_keep_their_real_time_for_merging_and_the_report(env):
    home, state, host, sent = env
    # Crashes on three different days are three incidents, not one storm.
    host.rows = [line("OWNER_COMMANDS_BLOCKED: TimeoutError", NOW - d * 86400) for d in (3, 2, 0)]
    sv.run(args(home, state, since_hours="96", report=True), host)
    text = "\n".join(sent)
    assert text.count("🚨 بوت تيليجرام طاح ورجع") == 3 and "تكرر" not in text
    assert "(2026-11-15" in text  # an old event says when it happened
    assert "بوت تيليجرام طاح ورجع: ١" in sent[-1]  # only today's crash is in the 24-hour report


def test_a_telegram_outage_defers_the_alert_to_the_next_run(env, monkeypatch):
    home, state, host, sent = env
    host.down.add("madar-v3-snap-commands.service")

    def outage(messages, dry):
        raise RuntimeError("Telegram rejected request: 502")

    monkeypatch.setattr(sv, "deliver", outage)
    sv.run(args(home, state), host)  # no crash
    assert (home / "undelivered.json").is_file()
    monkeypatch.setattr(sv, "deliver", lambda messages, dry: sent.extend(messages))
    sv.run(args(home, state, NOW + 60), host)
    assert any("خدمة واقفة" in m for m in sent)
    assert not (home / "undelivered.json").exists()
