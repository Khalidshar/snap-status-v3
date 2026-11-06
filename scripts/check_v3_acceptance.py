"""فحص قبول مدار بعد التشغيل من الشواهد المحلية، للقراءة فقط."""

import argparse
import hashlib
import json
import math
import sqlite3
import subprocess
from collections import Counter
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

APPS = {"snap": "سناب", "status": "واتساب"}
DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
STYLES = {"footage": "متحرك", "mix": "مختلط", "solid": "ثابت"}
LISTENERS = {app: f"madar-v3-{app}-commands.service" for app in APPS}
UNITS = {
    "مستمع سناب": LISTENERS["snap"],
    "مستمع واتساب": LISTENERS["status"],
    "ويبهوك واتساب": "madar-v3-status-command-webhook.service",
    **{
        f"{kind} {label}": unit
        for app, label in APPS.items()
        for kind, unit in (
            ("جلب الأخبار", f"madar-v3-{app}-news-egress.service"),
            ("جلب الفيديو", f"madar-v3-{app}-video.service"),
            ("جلب التلاوة", f"madar-v3-quran-egress@{app}.service"),
            ("جلب الإنتاج", f"madar-v3-production-egress@{app}.service"),
        )
    },
}
TABLES = {
    "jobs": ("idempotency_key", "error_code"),
    "commands": ("idempotency_key", None),
    "deliveries": ("id", "state"),
    "replies": ("key", "state"),
    "telegram_outbox": ("video_sha", "state"),
    "whatsapp_commands": ("tag", "state"),
    "whatsapp_outbox": ("job_id", "state"),
    "command_videos": ("tag", "state"),
    "panel_commands": ("id", "state"),
}
UNCERTAIN = (
    "UNKNOWN",
    "DELIVERY_UNKNOWN",
    "UPLOAD_UNKNOWN",
    "BLOCKED_OR_UNKNOWN",
    "ACK_EVIDENCE_PENDING",
    "SENDING",
    "SEND_INTENT",
    "UPLOAD_INTENT",
    "UPLOADING",
)


def safe_file(path: Path) -> bool:
    return path.is_file() and not any(item.is_symlink() for item in (path, *path.parents))


def read_json(path: Path) -> dict:
    if not safe_file(path):
        raise ValueError("unsafe file")
    with path.open("rb") as stream:
        data = stream.read(1_048_577)
    if len(data) > 1_048_576:
        raise ValueError("oversized report")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("invalid object")
    return result


def active(unit: str) -> bool:
    return (
        subprocess.run(  # noqa: S603 - fixed systemctl path and unit names
            ["/usr/bin/systemctl", "is-active", "--quiet", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        ).returncode
        == 0
    )


def timestamp(value: object) -> str:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("invalid timestamp")
    date = datetime.fromtimestamp(value, ZoneInfo("Asia/Riyadh"))
    weekday = ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد")
    return (
        f"{weekday[date.weekday()]} {date:%d-%m-%Y %I:%M:%S} {'ص' if date.hour < 12 else 'م'}"
    ).translate(DIGITS)


def database(path: Path) -> sqlite3.Connection:
    if not safe_file(path):
        raise ValueError("unsafe database")
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    if wal.is_symlink() or shm.is_symlink():
        raise ValueError("unsafe sidecar")
    # Avoid creating WAL shared-memory files while observing a stopped database.
    with path.open("rb") as stream:
        wal_mode = stream.read(20)[18:20] == b"\x02\x02"
    if wal_mode and wal.exists() and not safe_file(shm):
        raise ValueError("WAL snapshot unavailable")
    option = "mode=ro" if not wal_mode or wal.exists() else "mode=ro&immutable=1"
    return sqlite3.connect(f"{path.absolute().as_uri()}?{option}", uri=True, timeout=2)


def activity(root: Path, app: str) -> str:
    if app == "snap":
        for path in (root / "commands/listener-health.json", root / "listener-health.json"):
            if path.exists():
                return timestamp(read_json(path)["heartbeat"]) + " (نبض المستمع)"
        offset = root / "commands/offset.json"
        if type(read_json(offset).get("offset")) is not int:
            raise ValueError("invalid cursor")
        return timestamp(offset.stat().st_mtime) + " (آخر تحديث للمؤشر)"
    with closing(database(root / "commands/jobs.sqlite")) as db:
        last = db.execute("SELECT max(at) FROM whatsapp_commands").fetchone()[0]
    return timestamp(last) + " (آخر أمر وصل عبر الويبهوك)"


def latest_clip(root: Path, app: str) -> tuple[str, bool]:
    candidates = [
        path
        for path in root.rglob("report.json")
        if not path.parent.name.startswith("attempt-")
        and safe_file(path)
        and safe_file(path.with_name("video.mp4"))
    ]
    report = max(candidates, key=lambda item: item.stat().st_mtime)
    if report.with_name("video.mp4").stat().st_size == 0:
        raise ValueError("empty video")
    value = read_json(report)
    if value.get("app") != f"{app}-bot-v2":
        raise ValueError("report scope mismatch")
    qa, design = value.get("qa"), value.get("design")
    qa_ok = isinstance(qa, dict) and qa.get("result") == "PASS"
    seconds = qa.get("duration") if isinstance(qa, dict) else None
    duration = ""
    if type(seconds) in (int, float) and 0 < seconds <= 300:
        duration = f" · {seconds:.1f} ثانية".translate(DIGITS)
    style = design.get("style") if isinstance(design, dict) else None
    design_ok = (
        isinstance(design, dict)
        and design.get("agent") == "designer"
        and isinstance(style, str)
        and style in STYLES
        and isinstance(design.get("inspection"), dict)
        and design["inspection"].get("bars") == []
    )
    return (
        f"{timestamp(report.stat().st_mtime)} · الجودة {'ناجحة' if qa_ok else 'غير مؤكدة'}"
        f" · التصميم {'ناجح' if design_ok else 'غير مؤكد'}"
        f"{f' ({STYLES[style]})' if design_ok else ''}{duration}",
        qa_ok and design_ok,
    )


def ledger_counts(root: Path) -> tuple[int, int]:
    path = root / "events.jsonl"
    if not path.exists() and not path.is_symlink():
        return 0, 0
    if not safe_file(path):
        raise ValueError("unsafe ledger")
    intents: Counter[str] = Counter()
    outcomes: dict[str, str] = {}
    previous = "0" * 64
    with path.open("rb") as stream:
        for seq, line in enumerate(stream, start=1):
            if not line.endswith(b"\n"):
                raise ValueError("incomplete ledger")
            event = json.loads(line)
            signature = event.pop("hash")
            encoded = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
            if (
                event.get("seq") != seq
                or event.get("previous") != previous
                or hashlib.sha256(encoded).hexdigest() != signature
            ):
                raise ValueError("invalid ledger chain")
            previous = signature
            kind = event["kind"]
            if kind in (
                "INTENT",
                "UNKNOWN",
                "ACK_EVIDENCE_PENDING",
                "ACK",
                "PRE_NETWORK_BLOCKED",
                "REJECTED",
            ):
                digest = event["video_sha"]
                if not isinstance(digest, str):
                    raise ValueError("invalid ledger entry")
                intents[digest] += kind == "INTENT"
                outcomes[digest] = kind
    return (
        sum(kind in ("INTENT", "UNKNOWN", "ACK_EVIDENCE_PENDING") for kind in outcomes.values()),
        sum(count - 1 for count in intents.values() if count > 1),
    )


def queue_counts(root: Path, app: str) -> tuple[int, int]:
    unknown, duplicates = ledger_counts(root)
    scanned: set[str] = set()
    for path in root.rglob("*.sqlite"):
        if path.name not in ("jobs.sqlite", "replies.sqlite", "panel-commands.sqlite"):
            continue
        with closing(database(path)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            present = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table, (key, state) in TABLES.items():
                if table not in present:
                    continue
                scanned.add(table)
                duplicates += db.execute(
                    f"SELECT count(*) FROM (SELECT {key} FROM {table} "  # noqa: S608
                    f"GROUP BY {key} HAVING count(*) > 1)"
                ).fetchone()[0]
                if state:
                    placeholders = ",".join("?" for _ in UNCERTAIN)
                    unknown += db.execute(
                        f"SELECT count(*) FROM {table} WHERE {state} IN ({placeholders})",  # noqa: S608
                        UNCERTAIN,
                    ).fetchone()[0]
    if app == "status" and not {"whatsapp_commands", "whatsapp_outbox"} <= scanned:
        raise ValueError("missing WhatsApp queues")
    if app == "snap" and not scanned and not safe_file(root / "events.jsonl"):
        raise ValueError("missing Snap queues")
    return unknown, duplicates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, default=Path("/var/lib/madar"))
    args = parser.parse_args(argv)
    print("الفحص | النتيجة")
    print("----- | -------")
    ok = True
    for label, unit in UNITS.items():
        try:
            running = active(unit)
        except (OSError, subprocess.SubprocessError):
            running = False
        print(f"{label} | {'تعمل' if running else 'غير نشطة أو تعذر التحقق'}")
        ok &= running
    for app, label in APPS.items():
        root = args.state_root / app
        try:
            print(f"آخر إشارة {label} | {activity(root, app)}")
        except (OSError, ValueError, KeyError, OverflowError, sqlite3.Error):
            print(f"آخر إشارة {label} | غير متاحة")
            ok = False
        try:
            summary, passed = latest_clip(root, app)
            print(f"آخر مقطع {label} | {summary}")
            ok &= passed
        except (OSError, ValueError, OverflowError):
            print(f"آخر مقطع {label} | غير متاح أو تقريره غير صالح")
            ok = False
        try:
            unknown, duplicates = queue_counts(root, app)
            print(f"الطوابير {label} | إرسال غير محسوم {unknown}، طلبات مكررة {duplicates}")
            ok &= unknown == duplicates == 0
        except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
            print(f"الطوابير {label} | تعذر التحقق")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
