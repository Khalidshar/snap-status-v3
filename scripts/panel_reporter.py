"""One outbound WSL health report. No listener, shell, credentials in output or scheduler."""

import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def metrics() -> dict[str, object]:
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in {"MemTotal", "MemAvailable"}:
            memory[key] = int(value.split()[0]) * 1024
    disk = shutil.disk_usage(Path.home())
    services = {}
    for app in ("snap", "status"):
        try:
            result = subprocess.run(  # noqa: S603
                ["/usr/bin/systemctl", "is-active", f"madar-v3-{app}-commands.service"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            services[app] = "active" if result.returncode == 0 else "inactive"
        except (OSError, subprocess.SubprocessError):
            services[app] = "unknown"
    return {
        "disk_used": disk.used,
        "disk_total": disk.total,
        "memory_total": memory["MemTotal"],
        "memory_available": memory["MemAvailable"],
        "load": os.getloadavg()[0],
        "services": services,
    }


def main() -> None:
    endpoint = os.environ.get("MADAR_REPORTER_URL", "")
    token = os.environ.get("MADAR_REPORTER_TOKEN", "")
    url = urlsplit(endpoint)
    if (
        url.scheme != "https"
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
        data=json.dumps(metrics()).encode(),
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
