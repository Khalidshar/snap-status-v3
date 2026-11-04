"""One outbound server health report. No listener, shell, credentials in output or scheduler."""

import contextlib
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from studio_core.jobs import panel_schedule


def metrics() -> dict[str, object]:
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in {"MemTotal", "MemAvailable"}:
            memory[key] = int(value.split()[0]) * 1024
    disk = shutil.disk_usage(Path.home())
    services = {}
    units = {
        "snap": "madar-v3-snap-commands.service",
        "status": "madar-v3-status-commands.service",
        "snap_old": "snap-bot.timer",
        "status_old": "status-bot.timer",
    }
    for app, unit in units.items():
        try:
            result = subprocess.run(  # noqa: S603
                ["/usr/bin/systemctl", "is-active", unit],
                capture_output=True,
                timeout=5,
                check=False,
            )
            services[app] = "active" if result.returncode == 0 else "inactive"
        except (OSError, subprocess.SubprocessError):
            services[app] = "unknown"
    schedules = {}
    for app in ("snap", "status"):
        script = os.environ.get(f"MADAR_REPORTER_{app.upper()}_LEGACY_SCRIPT")
        state = os.environ.get(f"MADAR_REPORTER_{app.upper()}_LEGACY_STATE")
        if script:
            with contextlib.suppress(OSError, ValueError, SyntaxError, KeyError, TypeError):
                schedules[app] = panel_schedule.read(
                    app, Path(script), Path(state) if state else None
                )
    return {
        "schedules": schedules,
        "disk_used": disk.used,
        "disk_total": disk.total,
        "memory_total": memory["MemTotal"],
        "memory_available": memory["MemAvailable"],
        "load": os.getloadavg()[0],
        "services": services,
    }


def main() -> None:
    host = os.environ.get("MADAR_REPORTER_HOST", "wsl")
    endpoint = os.environ.get("MADAR_REPORTER_URL", "")
    token = os.environ.get("MADAR_REPORTER_TOKEN", "")
    url = urlsplit(endpoint)
    if (
        host not in {"amazon", "wsl"}
        or url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path != "/api/heartbeat"
        or url.query
        or url.fragment
        or len(token) < 32
    ):
        raise SystemExit("إعداد عميل الحالة غير مكتمل")
    req = urllib.request.Request(  # noqa: S310 - HTTPS and path validated above
        endpoint,
        data=json.dumps({"host": host, **metrics()}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )

    # Never follow redirects with the credential.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: object, **kwargs: object) -> None:
            return None

    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as response:
            if response.status != 202:
                raise ValueError("REPORT_REJECTED")
    except Exception:
        raise SystemExit("تعذّر إرسال حالة الجهاز؛ لا توجد إعادة تلقائية") from None
    print("تم إرسال حالة الجهاز")


if __name__ == "__main__":
    main()
