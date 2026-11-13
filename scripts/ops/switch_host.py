"""Fixed host-side operations for an explicitly approved, owner-operated cutover.

Never imported by either bot. Keys are stat'ed, never opened. No delete operation.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

STATE = Path("/var/lib/madar")
RELEASE = Path("/opt/madar/current")
MEDIA = {".mp4", ".mov", ".webm", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp3", ".wav", ".m4a"}
AUTOSTART = "madar-v3-autostart.service"
LIVE_DATABASES = tuple(
    f"{app}/{name}"
    for app in ("snap", "status")
    for name in (
        "jobs.sqlite",
        "commands/jobs.sqlite",
        "panel-commands.sqlite",
        "news/jobs.sqlite",
        "production/jobs.sqlite",
    )
)
LIVE_LOCKS = ("snap/lock", "status/lock")
LEGACY = ("snap-bot.service", "status-bot.service")
ACTIVE = {"active", "activating", "deactivating", "reloading"}
QUERIES = {
    "component_locks": "SELECT 1 FROM component_locks LIMIT 1",
    "jobs": (
        "SELECT 1 FROM jobs WHERE state NOT IN ('COMPLETED','FAILED','CANCELLED','EXPIRED') LIMIT 1"
    ),
    "command_videos": (
        "SELECT 1 FROM command_videos WHERE state IN "
        "('WORKING','UPLOAD_INTENT','SEND_INTENT') LIMIT 1"
    ),
    "panel_actions": (
        "SELECT 1 FROM panel_actions WHERE state IN ('QUEUED','INTENT','UNKNOWN') LIMIT 1"
    ),
}


def run(argv: list[str], *, timeout: int = 120) -> str:
    # Only fixed executables and validated argv; never pass output containing state to a log.
    return subprocess.run(  # noqa: S603
        argv, check=True, capture_output=True, text=True, timeout=timeout
    ).stdout.strip()


def units(release: Path = RELEASE) -> tuple[list[str], list[Path], list[str]]:
    text = (release / "scripts/ops/madar.sh").read_text()
    services = re.findall(r'"([^"|]+)\|', text.split("SERVICES=(", 1)[1].split("\n)", 1)[0])
    keys = re.findall(r"^  (/[^\n ]+)$", text.split("KEYS=(", 1)[1].split("\n)", 1)[0], re.M)
    tools = text.split("TOOLS=(", 1)[1].split(")", 1)[0].split()
    if len(services) != 13 or len(keys) != 8 or not tools:
        raise ValueError("SWITCH_INVENTORY_CHANGED")
    return [*services, "madar-v3-panel.service"], [Path(k) for k in keys], tools


def unit_states(names: list[str]) -> dict[str, str]:
    result = {}
    for name in names:
        result[name] = run(["systemctl", "show", name, "--property=ActiveState", "--value"])
        if result[name] not in {
            "active",
            "inactive",
            "failed",
            "activating",
            "deactivating",
            "reloading",
        }:
            raise ValueError("SWITCH_UNIT_STATE_UNKNOWN")
    return result


def workers() -> list[str]:
    result = run(["systemctl", "list-units", "--all", "--no-legend", "--plain", "--no-pager"])
    names = []
    for line in result.splitlines():
        name = line.split()[0]
        if name == AUTOSTART:
            autostart()  # Only an inert oneshot is exempt, never a boot process in flight.
            continue
        if name.startswith("madar-v3-") and name.endswith((".service", ".timer", ".socket")):
            if not re.fullmatch(r"[a-zA-Z0-9@_.\\:-]+", name):
                raise ValueError("SWITCH_UNIT_NAME_UNSAFE")
            names.append(name)
    return names


def path_safe(path: Path) -> None:
    if (
        not path.is_absolute()
        or ".." in path.parts
        or any(p.is_symlink() for p in (path, *path.parents))
    ):
        raise ValueError("SWITCH_PATH_UNSAFE")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest(root: Path, media: bool) -> dict[str, Any]:
    path_safe(root)
    if not root.is_dir():
        raise ValueError("SWITCH_STATE_MISSING")
    root_info = root.stat()
    result = {".": [root_info.st_mode & 0o7777, root_info.st_uid, root_info.st_gid, 0, None]}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        # SQLite's shared-memory index is rebuilt from DB+WAL. Read-only checks may
        # change its read marks; it is not durable state and must not cross hosts.
        if path.name.endswith("-shm") and path.with_name(path.name[:-4]).suffix in {
            ".sqlite",
            ".sqlite3",
            ".db",
        }:
            continue
        if stat.S_ISSOCK(info.st_mode):
            continue  # systemd recreates sockets; never transfer a live socket inode.
        if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
            raise ValueError("SWITCH_SPECIAL_FILE_REFUSED")
        if not media and path.suffix.lower() in MEDIA:
            continue
        name = path.relative_to(root).as_posix()
        if any(ord(c) < 32 for c in name):
            raise ValueError("SWITCH_FILENAME_UNSAFE")
        digest = file_digest(path) if path.is_file() else None
        result[name] = [
            info.st_mode & 0o7777,
            info.st_uid,
            info.st_gid,
            info.st_size if digest else 0,
            digest,
        ]
    return result


def idle(root: Path) -> None:
    path_safe(root)
    if not (root / "snap").is_dir() or not (root / "status").is_dir():
        raise ValueError("SWITCH_BOT_STATE_MISSING")
    for name in (*LIVE_LOCKS, *LIVE_DATABASES):
        path = root / name
        path_safe(path)
        if path.is_symlink():
            raise ValueError("SWITCH_STATE_SYMLINK")
        if path.name == "lock" and path.is_file():
            with path.open("rb") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("SWITCH_RENDER_LOCK_BUSY") from None
        if not path.is_file() or path.suffix not in {".sqlite", ".sqlite3", ".db"}:
            continue
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, query in QUERIES.items():
                if table in tables and db.execute(query).fetchone():
                    # An expired component row is UNKNOWN, never permission to erase it.
                    raise ValueError("SWITCH_PENDING_OR_BUSY")


def no_open_state(root: Path, proc: Path = Path("/proc")) -> None:
    # A manual writer outside systemd must also release the state before copying.
    prefix = str(root) + "/"
    for process in proc.iterdir():
        if not process.name.isdigit() or process.name == str(os.getpid()):
            continue
        try:
            for fd in (process / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                except FileNotFoundError:
                    continue  # file descriptor closed during this read-only inspection
                if target == str(root) or target.startswith(prefix):
                    raise ValueError("SWITCH_STATE_HANDLE_OPEN")
        except FileNotFoundError:
            continue  # process exited


def stopped(names: list[str], root: Path | None = None) -> None:
    states = unit_states(names)
    if any(s in ACTIVE for s in states.values()):
        raise ValueError("SWITCH_CONSUMER_STILL_RUNNING")
    if root is not None:
        no_open_state(root)
    # Refuse unsupervised consumers too; do not print their argv (may contain tokens).
    processes = run(["ps", "-eo", "args="])
    patterns = (
        "snap_bot.owner_commands",
        "status_bot.owner_commands",
        "cloudflared tunnel",
        "snap.py",
        "status_bot.command_webhook",
    )
    if any(p in processes for p in patterns):
        raise ValueError("SWITCH_UNSUPERVISED_CONSUMER")


def autostart() -> dict[str, str]:
    command = [
        "systemctl",
        "show",
        AUTOSTART,
        "--property=LoadState",
        "--property=Type",
        "--property=MainPID",
        "--property=ActiveState",
        "--property=UnitFileState",
    ]
    try:
        raw = run(command)
    except subprocess.CalledProcessError as error:
        if "LoadState=not-found" not in (error.stdout or ""):
            raise
        raw = error.stdout
    props = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    if props.get("LoadState") == "not-found":
        return {"enabled": "not-found", "state": "inactive"}
    if (
        props.get("LoadState") != "loaded"
        or props.get("Type") != "oneshot"
        or props.get("MainPID") != "0"
        or props.get("ActiveState") not in {"active", "inactive"}
    ):
        raise ValueError("SWITCH_AUTOSTART_NOT_INERT")
    enabled = props.get("UnitFileState", "")
    if enabled not in {
        "enabled",
        "enabled-runtime",
        "disabled",
        "static",
        "indirect",
        "masked",
        "masked-runtime",
    }:
        raise ValueError("SWITCH_AUTOSTART_STATE_UNKNOWN")
    return {"enabled": enabled, "state": props["ActiveState"]}


def boot_fence(enabled: str | None = None) -> dict[str, str]:
    before = autostart()
    if enabled is None:
        if before["enabled"] in {"enabled", "enabled-runtime"}:
            args = ["systemctl", "disable"]
            if before["enabled"] == "enabled-runtime":
                args.append("--runtime")
            run([*args, AUTOSTART])
        after = autostart()
        if after["enabled"] in {"enabled", "enabled-runtime"}:
            raise ValueError("SWITCH_AUTOSTART_DISABLE_FAILED")
    else:
        if enabled not in {
            "enabled",
            "enabled-runtime",
            "disabled",
            "static",
            "indirect",
            "masked",
            "masked-runtime",
            "not-found",
        }:
            raise ValueError("SWITCH_AUTOSTART_RESTORE_INVALID")
        if enabled in {"enabled", "enabled-runtime"}:
            args = ["systemctl", "enable"]
            if enabled == "enabled-runtime":
                args.append("--runtime")
            run([*args, AUTOSTART])
        after = autostart()
        if after["enabled"] != enabled:
            raise ValueError("SWITCH_AUTOSTART_RESTORE_FAILED")
    return after


def preflight(release: Path) -> dict[str, Any]:
    names, keys, tools = units(release)
    sizes = []
    for key in keys:
        info = key.stat()  # Do not open, parse, hash, copy or print a credential.
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise ValueError("SWITCH_KEY_MISSING")
        sizes.append(info.st_size)
    if any(not shutil.which(tool) for tool in [*tools, "rsync", "ssh"]):
        raise ValueError("SWITCH_TOOL_MISSING")
    raw = json.loads((release / "madar-release.json").read_text())
    # release.py writes commit under commit, not an inferred path or mutable branch name.
    if raw.get("environment") != "PASS":
        raise ValueError("SWITCH_RELEASE_UNVERIFIED")
    commit = raw.get("commit")
    if not isinstance(commit, str) or not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError("SWITCH_RELEASE_INVALID")
    return {"commit": commit, "key_sizes": sizes, "units": names}


def stage_path(root: Path, identifier: str) -> Path:
    if not re.fullmatch("[0-9a-f]{32}", identifier):
        raise ValueError("SWITCH_ID_INVALID")
    return root.with_name(root.name + ".transfer-" + identifier)


def action(
    operation: str, payload: dict[str, Any], root: Path = STATE, release: Path = RELEASE
) -> Any:
    names, _, _ = units(release)
    all_names = list(dict.fromkeys([*names, *LEGACY, *workers()]))
    if operation == "inspect":
        idle(root)
        info = preflight(release)
        info["states"] = unit_states(all_names)
        info["autostart"] = autostart()
        info["identity"] = hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest()
        return info
    if operation == "autostart-disable":
        stopped(all_names, root)
        return boot_fence()
    if operation == "autostart-restore":
        return boot_fence(payload["enabled"])
    if operation == "autostart-check":
        stopped(all_names, root)
        result = autostart()
        if result["enabled"] in {"enabled", "enabled-runtime"}:
            raise ValueError("SWITCH_SOURCE_BOOT_UNFENCED")
        return result
    if operation == "stop":
        idle(root)
        frozen = []
        try:
            # Freeze command producers/consumers before the final idle check. A job
            # accepted after inspection is detected before systemd may terminate it.
            for name, state in unit_states(all_names).items():
                if state in ACTIVE:
                    run(["systemctl", "freeze", name])
                    frozen.append(name)
            idle(root)
            for name in reversed(frozen):
                run(["systemctl", "stop", name])
            stopped(all_names, root)
            idle(root)
        finally:
            # On refusal resume the existing holders; never kill, steal or erase locks.
            for name in frozen:
                if unit_states([name])[name] in ACTIVE:
                    run(["systemctl", "thaw", name])
        return {"stopped": True}
    if operation == "manifest":
        stopped(all_names, root)
        idle(root)
        return manifest(root, bool(payload["media"]))
    if operation == "stage":
        stopped(all_names, root)
        stage = stage_path(root, payload["id"])
        path_safe(stage)
        stage.mkdir(mode=0o700)  # Exclusive; an earlier attempt remains as evidence.
        return {"staged": True}
    if operation == "promote":
        stopped(all_names, root)
        stage = stage_path(root, payload["id"])
        expected = dict(payload["manifest"])
        root_metadata = expected.pop(".", None)
        actual = manifest(stage, True)
        actual.pop(".")
        if actual != expected:
            raise ValueError("SWITCH_HASH_MISMATCH")
        if (
            not isinstance(root_metadata, list)
            or len(root_metadata) != 5
            or any(type(v) is not int for v in root_metadata[:3])
            or not 0 <= root_metadata[0] <= 0o7777
            or any(not 0 <= v < 2**32 - 1 for v in root_metadata[1:3])
            or root_metadata[3:] != [0, None]
        ):
            raise ValueError("SWITCH_ROOT_METADATA_INVALID")
        idle(stage)
        backup = root.with_name(root.name + ".before-" + payload["id"])
        path_safe(backup)
        if backup.exists():
            raise ValueError("SWITCH_BACKUP_EXISTS")
        # The private staging directory must regain the source root's ownership
        # and traversal mode before workers use it. Child hashes alone miss this.
        os.chown(stage, root_metadata[1], root_metadata[2])
        stage.chmod(root_metadata[0])
        root.rename(backup)
        try:
            stage.rename(root)
        except OSError:
            backup.rename(root)  # restore the old name if the second rename failed
            raise
        return {"promoted": True}
    if operation == "start":
        stopped([*LEGACY, *[n for n in all_names if n.endswith(".timer")]])
        requested = payload["units"]
        if not isinstance(requested, list) or any(n not in names for n in requested):
            raise ValueError("SWITCH_START_UNITS_INVALID")
        for name in names:
            if name in requested:
                run(["systemctl", "start", name])
                if unit_states([name])[name] != "active":
                    raise ValueError("SWITCH_SERVICE_START_FAILED")
        if any(s == "failed" for s in unit_states(all_names).values()):
            raise ValueError("SWITCH_FAILED_UNIT")
        return {"started": True}
    if operation == "preview":
        target = root.with_name(root.name + ".preview-" + payload["id"])
        path_safe(target)
        run(
            [
                str(release / ".venv/bin/python"),
                str(release / "scripts/generate_legacy_previews.py"),
                "--output",
                str(target),
                "--app",
                "both",
                "--commands",
                "news",
            ],
            timeout=7200,
        )
        rows = json.loads((target / "report.json").read_text())
        if len(rows) != 2 or {r["app"] for r in rows} != {"snap", "status"}:
            raise ValueError("SWITCH_PREVIEW_MISSING")
        for row in rows:
            seconds = float(row.get("duration", 0))
            if (
                row.get("result") != "PASS"
                or row.get("external_delivery") is not False
                or not (seconds >= 65 if row["app"] == "snap" else 0 < seconds <= 58)
            ):
                raise ValueError("SWITCH_PREVIEW_FAILED")
        if any(s != "active" for s in unit_states(names).values()):
            raise ValueError("SWITCH_SERVICE_NOT_ACTIVE")
        return {"previews": "PASS", "external_delivery": False}
    raise ValueError("SWITCH_OPERATION_INVALID")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=(
            "inspect",
            "stop",
            "manifest",
            "stage",
            "promote",
            "start",
            "preview",
            "autostart-disable",
            "autostart-restore",
            "autostart-check",
        ),
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, "SWITCH_ROOT_REQUIRED\n")
    try:
        payload = json.load(sys.stdin)
        print(json.dumps(action(args.operation, payload), ensure_ascii=False))
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        parser.exit(1, "SWITCH_HOST_STEP_FAILED; retain evidence and inspect locally\n")


if __name__ == "__main__":
    main()
