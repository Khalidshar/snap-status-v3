"""مشرف روّق — the bots' supervisor (owner 21 Nov 2026: «ابدا كامل المراحل»).

One pass per minute (a systemd timer, enabled only with the owner's approval):

1. Eyes: core services, failed units, blocked Snap sends, failed WhatsApp clips, stuck
   work, Telegram crashes, agent-chain stops, source refusals, disk and memory.
2. Memory: an incident ledger (SQLite) with first/last seen, count, duration, restarts.
3. Fast rules: a core service that is down is restarted once per incident; if it fails
   again within an hour it is never restarted automatically, only reported.
4. Repair requests: a written request (cause, evidence, owner) for code-level faults.
5. The owner: one Telegram message per batch, a reminder for long-open faults, a message
   when a fault clears, and a morning report. Duplicate faults are merged, never spammed.

Never edits code, deploys, sends clips or deletes evidence. Decisions come from code, not
from a model. Credentials are read by the Telegram helper only when sending.
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

RIYADH = ZoneInfo("Asia/Riyadh")
STATE = Path("/var/lib/madar")
HOME = STATE / "supervisor"

# Long-running services the bots need; restartable once per incident.
CORE = (
    "madar-v3-panel.service",
    "madar-v3-production-egress@snap.service",
    "madar-v3-production-egress@status.service",
    "madar-v3-quran-egress@snap.service",
    "madar-v3-quran-egress@status.service",
    "madar-v3-snap-commands.service",
    "madar-v3-snap-news-egress.service",
    "madar-v3-snap-video.service",
    "madar-v3-status-command-webhook.service",
    "madar-v3-status-commands.service",
    "madar-v3-status-news-egress.service",
    "madar-v3-status-video.service",
    "madar-v3-whatsapp-origin.service",
    "madar-v3-whatsapp-tunnel.service",
)
NAMES = {
    "madar-v3-snap-commands.service": "بوت تيليجرام (سناب)",
    "madar-v3-status-commands.service": "بوت واتساب",
    "madar-v3-status-command-webhook.service": "مستقبل رسائل واتساب",
    "madar-v3-whatsapp-tunnel.service": "نفق واتساب",
    "madar-v3-whatsapp-origin.service": "بوابة واتساب",
    "madar-v3-panel.service": "لوحة التحكم",
    "madar-v3-snap-video.service": "فيديو سناب",
    "madar-v3-status-video.service": "فيديو واتساب",
}
RESTART_COOLDOWN = 3600  # a second fall within an hour is reported, never restarted
STUCK_SECONDS = 25 * 60
REMIND_SECONDS = 6 * 3600
EVENT_MERGE_SECONDS = 3600  # the same event within an hour adds to one incident
SOURCE_REFUSALS_PER_HOUR = 40
MAX_LINES_PER_MESSAGE = 8
REPORT_HOUR = 8
HEARTBEAT_STALE = 10 * 60
SECRET = re.compile(
    r"\b\d{6,15}:[A-Za-z0-9_-]{30,80}\b|Bearer\s+\S+|(?i:token|secret|password)=\S+"
)
CODE = re.compile(r"\b[A-Z][A-Z0-9_]{5,}\b")
# Expected outcomes, not faults: no fresh story, nothing new in fashion.
BENIGN = {"NO_FRESH_CONTENT", "NO_FRESH_FASHION", "FASHION_UNAVAILABLE"}


@dataclass
class Finding:
    key: str
    kind: str  # service | stuck | condition | event
    title: str
    detail: str = ""
    severity: str = "high"  # high | info
    unit: str = ""
    request: bool = False  # write a repair request
    evidence: list[str] = field(default_factory=list)
    at: float = 0.0  # when the event happened (0: now)


def clean(text: str, limit: int = 300) -> str:
    return SECRET.sub("[محجوب]", " ".join(str(text).split()))[:limit]


def ar(n: int) -> str:
    return str(n).translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))


def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, RIYADH).strftime("%Y-%m-%d %H:%M")


def span(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return ar(minutes) + " دقيقة"
    hours, rest = divmod(minutes, 60)
    return ar(hours) + " ساعة" + (" و" + ar(rest) + " دقيقة" if rest else "")


class Probe:
    """Read-only views of the host; restart() is the one action, used by the rules."""

    def __init__(self, state: Path = STATE) -> None:
        self.state = state

    def run(self, *args: str, timeout: int = 30) -> str:
        done = subprocess.run(  # noqa: S603 — fixed argv, no shell
            list(args), capture_output=True, text=True, timeout=timeout, check=False
        )
        return done.stdout

    def active(self, unit: str) -> str:
        return self.run("systemctl", "is-active", unit).strip() or "unknown"

    def failed_units(self) -> list[tuple[str, str]]:
        names = self.run(
            "systemctl", "list-units", "--failed", "--no-legend", "--plain", "madar-v3*"
        ).split()
        units = [n for n in names if n.endswith(".service")]
        return [
            (u, self.run("systemctl", "show", "-p", "StateChangeTimestamp", "--value", u).strip())
            for u in units
        ]

    def running_since(self, pattern: str) -> list[tuple[str, float]]:
        names = self.run(
            "systemctl",
            "list-units",
            "--state=active,activating",
            "--no-legend",
            "--plain",
            pattern,
        ).split()
        result = []
        for unit in (n for n in names if n.endswith(".service")):
            raw = self.run(
                "systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "--value", unit
            ).strip()
            if raw.isdigit() and int(raw):
                uptime = time.clock_gettime(time.CLOCK_MONOTONIC)
                result.append((unit, time.time() - (uptime - int(raw) / 1e6)))
        return result

    def journal(self, cursor: str | None, since: float) -> tuple[list[dict[str, Any]], str | None]:
        args = ["journalctl", "-o", "json", "--no-pager", "-u", "madar-v3-*", "-n", "20000"]
        args += ["--after-cursor", cursor] if cursor else ["--since", f"@{int(since)}"]
        rows, last = [], cursor
        for line in self.run(*args, timeout=60).splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            last = row.get("__CURSOR", last)
            message = row.get("MESSAGE")
            if isinstance(message, list):  # journald sends non-UTF-8 as bytes
                message = bytes(message).decode("utf-8", "replace")
            rows.append(
                {
                    "at": int(row.get("__REALTIME_TIMESTAMP", 0)) / 1e6,
                    "unit": row.get("_SYSTEMD_UNIT", ""),
                    "message": str(message or ""),
                }
            )
        return rows, last

    def snap_events(self, after: int) -> list[dict[str, Any]]:
        path = self.state / "snap/events.jsonl"
        if not path.is_file():
            return []
        found = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row.get("seq"), int) and row["seq"] > after:
                    found.append(row)
        return found

    def status_videos(self) -> list[dict[str, Any]]:
        path = self.state / "status/commands/jobs.sqlite"
        if not path.is_file():
            return []
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(r) for r in db.execute("SELECT tag, kind, state, at, code FROM command_videos")
            ]

    def disk(self) -> float:
        stat = os.statvfs(str(self.state))
        return 1 - stat.f_bavail / stat.f_blocks

    def memory_mb(self) -> int:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
        return 10**6

    def restart(self, unit: str) -> bool:
        self.run("systemctl", "restart", unit, timeout=90)
        time.sleep(15)
        return self.active(unit) == "active"


class Ledger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS incidents(
              id INTEGER PRIMARY KEY, key TEXT NOT NULL, kind TEXT NOT NULL,
              severity TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL,
              unit TEXT NOT NULL DEFAULT '', opened_at REAL NOT NULL, last_seen REAL NOT NULL,
              count INTEGER NOT NULL DEFAULT 1, resolved_at REAL, alerted_at REAL,
              reminded_at REAL, restarts INTEGER NOT NULL DEFAULT 0, last_restart REAL,
              escalated INTEGER NOT NULL DEFAULT 0, request TEXT);
            CREATE INDEX IF NOT EXISTS incidents_key ON incidents(key, resolved_at);
            CREATE TABLE IF NOT EXISTS marks(name TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )

    def mark(self, name: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM marks WHERE name=?", (name,)).fetchone()
        return row["value"] if row else default

    def set_mark(self, name: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO marks VALUES(?,?)", (name, str(value)))

    def open_for(self, key: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.db.execute(
            "SELECT * FROM incidents WHERE key=? AND resolved_at IS NULL", (key,)
        ).fetchone()
        return row

    def recent(self, key: str, since: float) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.db.execute(
            "SELECT * FROM incidents WHERE key=? AND last_seen>=? ORDER BY id DESC LIMIT 1",
            (key, since),
        ).fetchone()
        return row

    def ever(self, key: str) -> bool:
        return bool(self.db.execute("SELECT 1 FROM incidents WHERE key=?", (key,)).fetchone())

    def add(self, f: Finding, now: float, resolved: bool = False) -> int:
        now = f.at or now
        cursor = self.db.execute(
            "INSERT INTO incidents(key,kind,severity,title,detail,unit,opened_at,last_seen,"
            "resolved_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f.key,
                f.kind,
                f.severity,
                f.title,
                f.detail,
                f.unit,
                now,
                now,
                now if resolved else None,
            ),
        )
        return int(cursor.lastrowid or 0)

    def touch(self, incident: int, now: float, detail: str | None = None) -> None:
        self.db.execute(
            "UPDATE incidents SET last_seen=?, count=count+1, detail=COALESCE(?, detail)"
            " WHERE id=?",
            (now, detail, incident),
        )

    def update(self, incident: int, **values: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in values)
        self.db.execute(f"UPDATE incidents SET {sets} WHERE id=?", (*values.values(), incident))  # noqa: S608

    def open_conditions(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM incidents WHERE resolved_at IS NULL AND kind!='event'"
        ).fetchall()

    def since(self, start: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM incidents WHERE last_seen>=? ORDER BY opened_at", (start,)
        ).fetchall()

    def commit(self) -> None:
        self.db.commit()


def eyes(probe: Probe, ledger: Ledger, now: float, first_since: float) -> list[Finding]:
    found: list[Finding] = []
    for unit in CORE:
        state = probe.active(unit)
        if state not in {"active", "activating", "reloading"}:
            name = NAMES.get(unit, unit.removesuffix(".service"))
            found.append(
                Finding(
                    "service:" + unit,
                    "service",
                    f"خدمة واقفة: {name}",
                    f"الحالة: {state}",
                    unit=unit,
                )
            )
    for unit, changed in probe.failed_units():
        # Render and send failures are reported once from their own evidence below
        # (journal code, Snap event log, WhatsApp queue); other units are reported here.
        if unit in CORE or "render" in unit or "send" in unit or "content@" in unit:
            continue
        found.append(
            Finding(
                f"failed:{unit}:{changed}",
                "event",
                "فشلت عملية في السيرفر",
                clean(unit.removesuffix(".service"), 90),
                unit=unit,
            )
        )
    for unit, started in probe.running_since("madar-v3-*render*"):
        if now - started > STUCK_SECONDS:
            found.append(
                Finding(
                    "stuck:" + unit,
                    "stuck",
                    "صنع مقطع متعلّق",
                    f"شغال من {span(now - started)}",
                    unit=unit,
                    request=True,
                )
            )

    seq = int(ledger.mark("snap_seq", "-1"))
    events = probe.snap_events(seq)
    if seq < 0:  # first pass: only events inside the first window count
        events_all, events = events, [e for e in events if float(e.get("time", 0)) >= first_since]
        last = max((e["seq"] for e in events_all), default=0)
    else:
        last = max((e["seq"] for e in events), default=seq)
    ledger.set_mark("snap_seq", last)
    for e in events:
        if e.get("kind") in {"PRE_NETWORK_BLOCKED", "UNKNOWN"}:
            found.append(
                Finding(
                    f"snap_send:{e.get('video_sha', '')[:16]}",
                    "event",
                    "إرسال سناب انمنع",
                    f"المقطع {str(e.get('video_sha', ''))[:8]} ما وصل تيليجرام"
                    + (" (النتيجة غير مؤكدة)" if e["kind"] == "UNKNOWN" else ""),
                    request=True,
                    at=float(e.get("time", 0)),
                )
            )

    seen_at = float(ledger.mark("status_at", str(first_since)))
    newest = seen_at
    for v in probe.status_videos():
        at = float(v["at"] or 0)
        newest = max(newest, at)
        state = str(v["state"] or "")
        if at > seen_at and ("BLOCK" in state or "FAIL" in state):
            code = str(v["code"] or "")
            benign = any(b in code for b in BENIGN)
            found.append(
                Finding(
                    f"status_clip:{v['tag']}",
                    "event",
                    "مقطع واتساب ما انرسل",
                    f"{v['kind']} — {clean(code, 80) or state}",
                    severity="info" if benign else "high",
                    request=not benign,
                    at=at,
                )
            )
        if state == "WORKING" and now - at > STUCK_SECONDS:
            found.append(
                Finding(
                    f"status_stuck:{v['tag']}",
                    "stuck",
                    "مقطع واتساب متعلّق",
                    f"{v['kind']} من {span(now - at)}",
                    request=True,
                )
            )
    ledger.set_mark("status_at", newest)

    rows, cursor = probe.journal(ledger.mark("journal") or None, first_since)
    if cursor:
        ledger.set_mark("journal", cursor)
    minutes: dict[str, int] = json.loads(ledger.mark("refusals", "{}"))
    for row in rows:
        message = row["message"]
        if "OWNER_COMMANDS_BLOCKED" in message:
            reason = message.split(":", 1)[-1].strip()
            found.append(
                Finding(
                    "telegram_crash",
                    "event",
                    "بوت تيليجرام طاح ورجع",
                    clean(reason, 60),
                    request=True,
                    at=row["at"],
                )
            )
        elif "AGENT_CHAIN" in message:
            found.append(
                Finding(
                    "agent_chain",
                    "event",
                    "سلسلة الوكلاء الـ١٨ وقفت",
                    clean(message, 160),
                    request=True,
                    at=row["at"],
                )
            )
        elif "SNAP_PREVIEW_FAILURE" in message:
            stage = re.search(r"stage=(\w+)", message)
            code_match = re.search(r"code=(\w+)", message)
            stage_name = stage.group(1) if stage else ""
            code_name = code_match.group(1) if code_match else "غير معروف"
            # Owner 22 Nov 2026: the internal reason code, when the worker gave one.
            reason_match = re.search(r"reason=([A-Z][A-Z0-9_]{2,63})", message)
            reason = " — السبب: " + reason_match.group(1) if reason_match else ""
            # A blocked send is reported from the Snap event log; no story is not a fault.
            if stage_name == "send" or code_name in BENIGN:
                continue
            found.append(
                Finding(
                    f"clip_fail:{code_name}",
                    "event",
                    "فشل صنع مقطع سناب",
                    f"المرحلة {stage_name or '—'} — {code_name}{reason}",
                    request=True,
                    at=row["at"],
                )
            )
        elif "PRODUCTION_SOURCE_" in message or "FETCH_FAILED" in message:
            minute = str(int(row["at"] // 60))
            minutes[minute] = minutes.get(minute, 0) + 1
    floor = int(now // 60) - 60
    minutes = {m: n for m, n in minutes.items() if int(m) > floor}
    ledger.set_mark("refusals", json.dumps(minutes))
    refusals = sum(minutes.values())
    if refusals >= SOURCE_REFUSALS_PER_HOUR:
        found.append(
            Finding(
                "sources",
                "condition",
                "مصادر ترفض كثير",
                f"{ar(refusals)} رفض في آخر ساعة",
                severity="info",
            )
        )
    if probe.disk() > 0.85:
        found.append(Finding("disk", "condition", "القرص قرب يمتلي", f"{probe.disk():.0%}"))
    if probe.memory_mb() < 300:
        found.append(
            Finding("memory", "condition", "الذاكرة قليلة", f"{ar(probe.memory_mb())} ميجا")
        )
    return found


def write_request(home: Path, f: Finding, incident: int, now: float, count: int) -> str:
    folder = home / "requests"
    folder.mkdir(parents=True, exist_ok=True, mode=0o750)
    name = datetime.fromtimestamp(now, RIYADH).strftime("%Y%m%d-%H%M") + f"-{incident}.md"
    lines = [
        f"# طلب إصلاح: {f.title}",
        "",
        f"- الوقت: {clock(now)} (الرياض)",
        f"- التفاصيل: {clean(f.detail)}",
        f"- عدد المرات: {count}",
        f"- الخدمة: {f.unit or '—'}",
        "- المسؤول: شيهانة (التشخيص والإصلاح على فرع، والنشر بموافقة خالد)",
        "- المشرف لم يعدّل أي كود ولم يُعِد إرسال أي مقطع.",
    ]
    if f.unit:
        lines += ["", "للفحص:", "```", f"journalctl -u {f.unit} -n 60 --no-pager", "```"]
    (folder / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return name


def think(
    ledger: Ledger, probe: Probe, found: list[Finding], now: float, home: Path, act: bool
) -> list[str]:
    """Record, apply the restart rule, and return the owner's message lines."""
    lines: list[str] = []
    current = {f.key for f in found}
    for f in found:
        if f.kind == "event":
            one_off = f.key.startswith(("snap_send:", "status_clip:", "failed:"))
            if one_off and ledger.ever(f.key):
                continue
            if not one_off:  # recurring events within an hour join one incident
                when = f.at or now
                merged = ledger.recent(f.key, when - EVENT_MERGE_SECONDS)
                if merged:
                    ledger.touch(merged["id"], max(when, merged["last_seen"]), f.detail)
                    if merged["count"] + 1 in {3, 10}:
                        lines.append(f"🔁 {f.title}: تكرر {ar(merged['count'] + 1)} مرات خلال ساعة")
                    continue
            incident = ledger.add(f, now, resolved=True)
            note = ""
            if f.request and f.severity == "high":
                ledger.update(incident, request=write_request(home, f, incident, f.at or now, 1))
                note = " 📝"
            if f.at and now - f.at > 600:  # an older event: say when it happened
                note = f" ({clock(f.at)})" + note
            if f.severity == "high":
                lines.append(f"🚨 {f.title} — {f.detail}{note}")
                ledger.update(incident, alerted_at=now)
            continue
        row = ledger.open_for(f.key)
        if row is None:
            incident = ledger.add(f, now)
            line = f"🚨 {f.title} — {f.detail}"
            if f.kind == "service":
                last = ledger.db.execute(
                    "SELECT MAX(last_restart) FROM incidents WHERE key=?", (f.key,)
                ).fetchone()[0]
                if last and now - float(last) < RESTART_COOLDOWN:
                    line += " — طاحت مرة ثانية خلال ساعة، ما أعيد تشغيلها. تحتاج فحص."
                    ledger.update(incident, escalated=1)
                elif act:
                    ok = probe.restart(f.unit)
                    ledger.update(incident, restarts=1, last_restart=now)
                    line += " — أعدت تشغيلها ✅ ورجعت" if ok else " — أعدت تشغيلها وما رجعت ❌"
                else:
                    line += " — (تجربة: كان بيعيد تشغيلها مرة وحدة)"
            if f.request and f.severity == "high":
                ledger.update(incident, request=write_request(home, f, incident, now, 1))
                line += " 📝"
            if f.severity == "high":
                lines.append(line)
                ledger.update(incident, alerted_at=now)
        else:
            ledger.touch(row["id"], now, f.detail)
            if row["severity"] == "high" and now - float(row["reminded_at"] or row["opened_at"]) > (
                REMIND_SECONDS
            ):
                lines.append(f"⏰ لسا قائم: {row['title']} — من {span(now - row['opened_at'])}")
                ledger.update(row["id"], reminded_at=now)
    for row in ledger.open_conditions():
        if row["key"] not in current:
            ledger.update(row["id"], resolved_at=now)
            if row["severity"] == "high":
                lines.append(f"✅ انحل: {row['title']} — بعد {span(now - row['opened_at'])}")
    return lines


def compose(lines: list[str]) -> list[str]:
    """Batch lines into few messages; duplicates collapse with a count."""
    counts: dict[str, int] = {}
    for line in lines:
        counts[line] = counts.get(line, 0) + 1
    unique = [line + (f" ({ar(n)} مرات)" if n > 1 else "") for line, n in counts.items()]
    if not unique:
        return []
    head = "🛡️ مشرف روّق"
    messages = []
    for start in range(0, len(unique), MAX_LINES_PER_MESSAGE):
        chunk = unique[start : start + MAX_LINES_PER_MESSAGE]
        messages.append(head + "\n" + "\n".join(chunk))
    if len(unique) > MAX_LINES_PER_MESSAGE * 2:
        rest = len(unique) - MAX_LINES_PER_MESSAGE * 2
        messages = messages[:2]
        messages[-1] += f"\n… و{ar(rest)} غيرها في السجل"
    return messages


def report(ledger: Ledger, now: float, home: Path) -> str:
    rows = ledger.since(now - 86400)
    lines = ["🛡️ تقرير المشرف — آخر ٢٤ ساعة"]
    if not rows:
        return lines[0] + "\n✅ ما فيه أي عطل. البوتين شغالين."
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row["title"], []).append(row)
    for title, items in sorted(groups.items(), key=lambda kv: -sum(r["count"] for r in kv[1])):
        total = sum(r["count"] for r in items)
        lines.append(f"• {title}: {ar(total)}")
    still = [r for r in ledger.open_conditions() if r["severity"] == "high"]
    if still:
        lines.append("⚠️ لسا قائم: " + "، ".join(r["title"] for r in still))
    restarts = sum(r["restarts"] for r in rows)
    if restarts:
        lines.append(f"🔄 إعادة تشغيل: {ar(restarts)}")
    requests = [r["request"] for r in rows if r["request"]]
    if requests:
        lines.append(f"📝 طلبات إصلاح مكتوبة: {ar(len(requests))} (في {home / 'requests'})")
    return "\n".join(lines)


def deliver(messages: list[str], dry: bool) -> None:
    if dry:
        for message in messages:
            print("— رسالة —\n" + message + "\n")
        return
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/snap-bot"))
    from snap_bot.server_preview import channel_bot
    from snap_bot.telegram_egress import api, channel_credentials

    # The main bot as the installer pinned it in the release manifest.
    token, chat, _ = channel_credentials("main", channel_bot("main"))
    for message in messages:
        api(token, "sendMessage", json.dumps({"chat_id": chat, "text": message}).encode())


def run(args: argparse.Namespace, probe: Probe | None = None) -> list[str]:
    home = Path(args.home)
    now = float(args.now or time.time())
    ledger = Ledger(home / "incidents.sqlite")
    probe = probe or Probe(Path(args.state))
    first = now - float(args.since_hours) * 3600
    found = eyes(probe, ledger, now, first)
    lines = think(ledger, probe, found, now, home, act=not args.dry_run and not args.no_restart)
    messages = compose(lines)
    day = datetime.fromtimestamp(now, RIYADH)
    if args.report or (
        day.hour == REPORT_HOUR and ledger.mark("report_day") != day.date().isoformat()
    ):
        messages.append(report(ledger, now, home))
        ledger.set_mark("report_day", day.date().isoformat())
    ledger.set_mark("heartbeat", now)
    ledger.commit()
    if not args.dry_run or args.print:
        # Owner 22 Nov 2026: a brief Telegram outage must not lose an alert or crash the run.
        pending = home / "undelivered.json"
        waiting: list[str] = []
        if not args.dry_run and pending.is_file():
            try:
                waiting = [str(m) for m in json.loads(pending.read_text(encoding="utf-8"))]
            except (OSError, ValueError):
                waiting = []
        try:
            deliver(waiting + messages, args.dry_run)
            if waiting:
                pending.unlink(missing_ok=True)
        except (RuntimeError, OSError):
            print("SUPERVISOR_DELIVER_DEFERRED", file=sys.stderr, flush=True)
            pending.write_text(json.dumps((waiting + messages)[-20:], ensure_ascii=False))
    return messages


def watchdog(args: argparse.Namespace) -> list[str]:
    """The supervisor's own heartbeat: an hourly timer tells the owner if it went silent."""
    path = Path(args.home) / "incidents.sqlite"
    now = float(args.now or time.time())
    beat = 0.0
    if path.is_file():
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            row = db.execute("SELECT value FROM marks WHERE name='heartbeat'").fetchone()
            beat = float(row[0]) if row else 0.0
    if now - beat <= HEARTBEAT_STALE:
        return []
    message = "🛡️ مشرف روّق وقف عن الفحص" + (
        f" من {span(now - beat)}" if beat else " (ما سجّل أي فحص)"
    )
    deliver([message], args.dry_run)
    return [message]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("run", "watchdog"))
    parser.add_argument("--home", default=str(HOME))
    parser.add_argument("--state", default=str(STATE))
    parser.add_argument("--dry-run", action="store_true", help="no message, no restart")
    parser.add_argument("--print", action="store_true", help="print messages in a dry run")
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--report", action="store_true", help="add the daily report now")
    parser.add_argument("--since-hours", default="1", help="first-run window")
    parser.add_argument("--now", default="")
    args = parser.parse_args(argv)
    if args.action == "watchdog":
        watchdog(args)
    else:
        run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
