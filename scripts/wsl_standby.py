"""Manual WSL failover for an already installed v3 release; no install/schedule."""

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path("/var/lib/madar")
RELEASE = Path("/opt/madar/current")
# Long-running command services only. No render/send template or historical unit.
UNITS = {
    "snap": ("madar-v3-snap-news-egress.service", "madar-v3-snap-commands.service"),
    "status": (
        "madar-v3-status-news-egress.service",
        "madar-v3-status-command-webhook.service",
        "madar-v3-status-commands.service",
    ),
}


def systemctl(*args: str) -> bool:
    return (
        subprocess.run(  # noqa: S603
            ["/usr/bin/systemctl", *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
            check=False,
        ).returncode
        == 0
    )


def prerequisite(app: str) -> None:
    if os.geteuid() != 0 or "microsoft" not in platform.release().lower():
        raise ValueError("يلزم تشغيل السكربت بصلاحية الجذر داخل WSL")
    if not Path("/run/systemd/system").is_dir():
        raise ValueError("يلزم وجود systemd مفعّل في WSL")
    manifest = json.loads((RELEASE / "madar-release.json").read_text())
    commit = manifest.get("commit", "")
    if not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError("بيان الإصدار غير صالح")
    evidence = ROOT / app / "standby-restore.json"
    if evidence.is_symlink() or evidence.stat().st_uid != 0 or evidence.stat().st_mode & 0o077:
        raise ValueError("دليل الاستعادة غير محمي")
    restore = json.loads(evidence.read_text())
    if restore.get("commit") != commit or restore.get("primary_stopped") is not True:
        raise ValueError("الاستعادة لا تطابق الإصدار أو توقف الأساسي غير موثّق")
    files = restore.get("files", {})
    if not files:
        raise ValueError("دليل الاستعادة خالٍ من البصمات")
    for name, digest in files.items():
        path = ROOT / app / name
        if (
            not isinstance(name, str)
            or Path(name).is_absolute()
            or ".." in Path(name).parts
            or any(p.is_symlink() for p in (path, *path.parents))
        ):
            raise ValueError("مسار استعادة مرفوض")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("بصمة الحالة المستعادة غير مطابقة")
    for unit in UNITS[app]:
        if not systemctl("cat", unit):
            raise ValueError("وحدة مطلوبة غير مثبتة")


def event(app: str, action: str, unit: str = "") -> None:
    # Append only; no exception contents, paths supplied by users or credentials.
    with (ROOT / app / "standby-audit.jsonl").open("a") as stream:
        stream.write(
            json.dumps({"time": datetime.now(UTC).isoformat(), "action": action, "unit": unit})
            + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())


def operate(app: str, action: str, primary_fenced: bool = False) -> dict[str, Any]:
    if action in {"check", "start"}:
        prerequisite(app)
    if action == "check":
        return {"ready": True, "started": [], "units": UNITS[app]}
    if action == "status":
        return {"units": {u: systemctl("is-active", "--quiet", u) for u in UNITS[app]}}
    if os.geteuid() != 0:
        raise ValueError("يلزم تشغيل السكربت بصلاحية الجذر")
    if action == "start" and not primary_fenced:
        raise ValueError("يلزم تأكيد إيقاف الأساسي ومنع إعادة تشغيله قبل التحويل")
    state = ROOT / app
    lock = state / "standby.lock"
    record = state / "standby-started.json"
    if any(p.is_symlink() for p in (state, lock, record, state / "standby-audit.jsonl")):
        raise ValueError("مسار حالة غير آمن")
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if action == "stop":
            owned = json.loads(record.read_text()) if record.exists() else []
            if not isinstance(owned, list) or any(u not in UNITS[app] for u in owned):
                raise ValueError("سجل تشغيل غير صالح")
            for unit in reversed(owned):
                if not systemctl("stop", unit):
                    event(app, "stop_failed", unit)
                    raise ValueError("تعذر الإيقاف؛ راجع حالة الخدمات قبل تشغيل الأساسي")
                event(app, "stopped", unit)
            record.write_text("[]")
            return {"stopped": owned}
        if record.exists() and json.loads(record.read_text()):
            raise ValueError("يوجد تشغيل احتياطي مسجّل؛ افحصه قبل أي إعادة")
        if any(systemctl("is-active", "--quiet", u) for u in UNITS[app]):
            raise ValueError("خدمة تعمل مسبقًا؛ لا يُسمح بامتلاك تشغيل مجهول")
        started: list[str] = []
        for unit in UNITS[app]:
            # Record the attempt before systemctl: a failed start may still leave a process.
            started.append(unit)
            record.write_text(json.dumps(started))
            event(app, "start_requested", unit)
            if not systemctl("start", unit) or not systemctl("is-active", "--quiet", unit):
                event(app, "start_failed", unit)
                # Preserve attempted ownership so stop can reconcile the whole attempt.
                raise ValueError("فشل التشغيل؛ نفّذ stop وافحص السجل، لا تعد المحاولة تلقائيًا")
            event(app, "started", unit)
        return {"started": started}


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="تشغيل احتياطي يدوي داخل WSL")
    parser.add_argument(
        "action", nargs="?", default="check", choices=["check", "start", "stop", "status"]
    )
    parser.add_argument("--app", required=True, choices=list(UNITS))
    parser.add_argument("--primary-fenced", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(operate(args.app, args.action, args.primary_fenced), ensure_ascii=False))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Only our own fixed ValueError messages can reach the console.
        print(str(error) if type(error) is ValueError else "تعذر التنفيذ؛ لم تُعرض تفاصيل حساسة")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
