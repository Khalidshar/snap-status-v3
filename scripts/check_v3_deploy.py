"""Read-only deployment preflight. Environment file contents never leave this process."""

import argparse
import os
import shutil
import stat
import subprocess
from pathlib import Path

ENV_NAMES = ("snap-telegram-main.env", "status-whatsapp.env", "media.env", "quran.env")
UNITS = (
    "snap-bot.timer",
    "status-bot.timer",
    "khalid-command-bridge.service",
    "madar-v3-snap-commands.service",
    "madar-v3-status-commands.service",
    "madar-v3-status-command-webhook.service",
    "madar-v3-snap-video.service",
    "madar-v3-status-video.service",
    "madar-v3-snap-news-egress.service",
    "madar-v3-status-news-egress.service",
    "madar-v3-quran-egress@snap.service",
    "madar-v3-quran-egress@status.service",
)


def env_sizes(path: Path) -> tuple[int, int, int]:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError("unsafe environment file")
        raw = stream.read(65537)
    if not raw.strip() or len(raw) > 65536:
        raise ValueError("invalid environment file size")
    lengths = [len(line) for line in raw.splitlines()]
    return len(lengths), min(lengths), max(lengths)


def unit_loaded(unit: str) -> bool:
    result = subprocess.run(  # noqa: S603 - fixed binary and unit list
        ["/usr/bin/systemctl", "show", "--property=LoadState", "--value", unit],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    return result.returncode == 0 and result.stdout.strip() == "loaded"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-free-gib", type=int, default=5)
    args = parser.parse_args()
    if args.min_free_gib < 1:
        parser.error("minimum free space must be positive")
    ok = True
    for name in ENV_NAMES:
        try:
            lines, shortest, longest = env_sizes(Path("/etc/madar") / name)
            print(f"{name}: {lines} أسطر، أقصرها {shortest} بايت وأطولها {longest} بايت")
        except (OSError, ValueError):
            print(f"{name}: غائب أو غير قابل للفحص الآمن")
            ok = False
    for unit in UNITS:
        try:
            present = unit_loaded(unit)
        except (OSError, subprocess.SubprocessError):
            present = False
        print(f"{unit}: {'مثبتة' if present else 'غير متاحة'}")
        ok &= present
    for path in (
        Path("/opt/madar/current/.venv/bin/python"),
        Path("/opt/madar/current/madar-release.json"),
        Path("/opt/madar/current/config/quran/tafsir-reference.json"),
        Path("/etc/madar/status-command-webhook.json"),
    ):
        present = path.is_file()
        print(f"{path}: {'موجود' if present else 'غائب'}")
        ok &= present
    for path in map(Path, ("/opt/madar/releases", "/var/lib/madar/snap", "/var/lib/madar/status")):
        try:
            available = shutil.disk_usage(path).free
            print(f"{path}: {available // (1024**3)} غيغابايت متاحة")
            ok &= path.is_dir() and available >= args.min_free_gib * 1024**3
        except OSError:
            print(f"{path}: تعذر فحص المساحة")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
