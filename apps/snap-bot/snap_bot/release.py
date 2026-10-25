"""Immutable v3 releases beside the lab (V2) releases. No service start or enable.

Ported from the lab (snap-bot-v2 tools/install_madar_release.py). Differences:
- the workspace has no third-party dependencies, so the release venv carries no pip and a
  single .pth file puts the release's own studio-core, snap-bot and status-bot on the path;
- installing never activates: switching /opt/madar/current is the separate cutover step
  (--activate-only), and the same command switches back to a verified lab release;
- v3 has its own unit names. Lab units fail closed on a v3 release (no snap_app module)
  and v3 units fail closed on a lab release (no snap_bot module).
The Snap state directory and its append-only ledger stay shared, so a video SHA consumed by
either version stays consumed for both. Status keeps its own state directory and has a
render-only unit: no Status delivery unit exists.
The owner's main bot is opt-in: --telegram-main-env and --telegram-main-bot together add a
separate madar-v3-snap-send-main@ unit and record the pinned bot in the release manifest.
The test unit is unchanged, so the main bot never receives a send meant for the test bot.

Stdlib only: it runs from a clean source checkout before any venv exists:
  sudo python3 apps/snap-bot/snap_bot/release.py --source <checkout> --commit <40-hex> \
      --node-bin <node 22> --telegram-env <existing env file> --dry-run
"""

import argparse
import json
import os
import pwd
import re
import subprocess
import tomllib
import uuid
from pathlib import Path
from typing import Any

BASE = Path("/opt/madar")
RELEASES = BASE / "releases"
STATE = Path("/var/lib/madar")
DATA = STATE / "snap"
STATUS_DATA = STATE / "status"
UNIT_DIR = Path("/etc/systemd/system")
UNITS = (
    "madar-v3-snap-render.service",
    "madar-v3-snap-send@.service",
    "madar-v3-status-render.service",
)
MAIN_UNIT = "madar-v3-snap-send-main@.service"
TEST_BOT = "khalid_snap_v2_test_bot"
BOT_NAME = r"[A-Za-z][A-Za-z0-9_]{1,29}[Bb][Oo][Tt]"
LAYOUT = "v3"
PYTHON = "python3.12"
SITE = ".venv/lib/python3.12/site-packages"
PATH_FILE = SITE + "/madar-release.pth"
IMPORT_ROOTS = ("apps/snap-bot", "apps/status-bot", "packages/studio-core")
REQUIRED = (
    "apps/snap-bot/snap_bot/server_preview.py",
    "apps/snap-bot/snap_bot/check_env.py",
    "apps/status-bot/status_bot/server_preview.py",
    "packages/studio-core/studio_core/__init__.py",
)
# Exact runtime dependencies: anything else would need a hash-locked install step.
DEPENDENCIES = {
    "apps/snap-bot/pyproject.toml": ["studio-core"],
    "apps/status-bot/pyproject.toml": ["studio-core"],
    "packages/studio-core/pyproject.toml": [],
}


def destination(path: str | Path, allow_current: bool = False) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Destination outside allowed roots")
    allowed = path in (BASE, STATE) or BASE in path.parents or STATE in path.parents
    unit = path.parent == UNIT_DIR and path.name in (*UNITS, MAIN_UNIT)
    if not (allowed or unit):
        raise ValueError("Destination outside allowed roots")
    for item in (path, *path.parents):
        if item.is_symlink():
            current = item == BASE / "current" and item == path
            if allow_current and current and item.resolve().parent == RELEASES:
                continue
            raise ValueError("Symlink destination rejected")
    return path


def git(source: Path, *args: str) -> bytes:
    command = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false"]
    command += ["-c", "core.hooksPath=/dev/null", *args]
    env = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
    return subprocess.check_output(command, cwd=source, env=env)  # noqa: S603  # fixed argv


def source_files(source: Path, commit: str) -> list[str]:
    if git(source, "rev-parse", "HEAD").decode().strip() != commit:
        raise ValueError("Source commit mismatch")
    if git(source, "status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("Tracked source dirty")
    files = git(source, "ls-files", "-z").decode().split("\0")[:-1]
    skipped = (".git", ".venv", ".install-tmp")
    for name in files:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or any(x in skipped for x in path.parts):
            raise ValueError("Unsafe source path")
        if name == "madar-release.json":
            raise ValueError("Unsafe source path")
        if (source / path).is_symlink() or not (source / path).is_file():
            raise ValueError("Unsafe source file")
    return files


def check_dependencies(source: Path, files: list[str]) -> None:
    for name in REQUIRED:
        if name not in files:
            raise ValueError("Release layout incomplete")
    for name, expected in DEPENDENCIES.items():
        if name not in files:
            raise ValueError("Release layout incomplete")
        project = tomllib.loads((source / name).read_text())["project"]
        if project.get("dependencies", []) != expected:
            raise ValueError("Unexpected runtime dependencies")


def telegram_file(path: str | Path) -> Path:
    # The file holds the bot token: it is checked to exist, never opened or printed.
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or Path("/etc") not in path.parents:
        raise ValueError("Telegram environment file must be under /etc")
    if any(item.is_symlink() for item in (path, *path.parents)) or not path.is_file():
        raise ValueError("Telegram environment file missing")
    if path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
        raise ValueError("Untrusted Telegram environment file")
    return path


def main_bot(value: str) -> str:
    # Mirrors telegram_egress.main_bot; this installer runs before any venv exists.
    if not re.fullmatch(BOT_NAME, value) or value.lower() == TEST_BOT:
        raise ValueError("Main bot username required")
    return value


def main_unit_text(telegram_env: str) -> str:
    """Delivery to the owner's main bot. The bot it must be is pinned in the release manifest."""
    return unit_text(True, telegram_env).replace(
        " send --video-sha %i\n", " send --channel main --video-sha %i\n"
    )


def unit_text(send: bool = False, telegram_env: str = "") -> str:
    common = """[Unit]
Description=Madar v3 manual Snap operation
[Service]
Type=oneshot
User=root
UMask=0077
WorkingDirectory=/opt/madar/current
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=TMPDIR=/var/lib/madar/snap/tmp
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
RestrictSUIDSGID=yes
LockPersonality=yes
CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_SETUID CAP_SETGID
ReadWritePaths=/var/lib/madar
TimeoutStartSec=300
MemoryMax=1G
CPUQuota=50%
UnsetEnvironment=GITHUB_TOKEN GITHUB_REPOSITORY GITHUB_ACTOR GITHUB_EVENT_NAME PYTHONPATH
"""
    if send:
        if not telegram_env:
            raise ValueError("Telegram environment file required")
        return (
            common
            + "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n"
            + "EnvironmentFile="
            + telegram_env
            + "\n"
            + "ExecStart=/opt/madar/current/.venv/bin/python -m snap_bot.server_preview"
            + " send --video-sha %i\n"
        )
    return common + (
        "PrivateNetwork=yes\n"
        "RestrictAddressFamilies=AF_UNIX\n"
        "UnsetEnvironment=TG_TEST_TOKEN TG_TEST_CHAT\n"
        "ExecStart=/opt/madar/current/.venv/bin/python -m snap_bot.server_preview render\n"
    )


def status_unit_text() -> str:
    # Controller only: the egress and renderer run as separate transient units.
    return """[Unit]
Description=Madar v3 manual Status weather preview (no delivery)
[Service]
Type=oneshot
User=root
UMask=0077
WorkingDirectory=/opt/madar/current
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=TMPDIR=/var/lib/madar/status/tmp
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
PrivateNetwork=yes
RestrictAddressFamilies=AF_UNIX
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
RestrictSUIDSGID=yes
LockPersonality=yes
CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE
ReadWritePaths=/var/lib/madar/status
TimeoutStartSec=600
MemoryMax=1G
CPUQuota=50%
UnsetEnvironment=GITHUB_TOKEN GITHUB_REPOSITORY GITHUB_ACTOR GITHUB_EVENT_NAME PYTHONPATH TG_TEST_TOKEN TG_TEST_CHAT
ExecStart=/opt/madar/current/.venv/bin/python -m status_bot.server_preview render
"""


def unit_for(name: str, telegram_env: str) -> str:
    if name.startswith("madar-v3-status-"):
        return status_unit_text()
    return unit_text(name.endswith("@.service"), telegram_env)


def path_file(release: Path) -> str:
    return "".join(str(release / name) + "\n" for name in IMPORT_ROOTS)


def build_plan(
    source: str | Path,
    commit: str,
    node_bin: str | Path,
    telegram_env: str,
    owner: str = "ubuntu",
    main_env: str = "",
    main_bot_name: str = "",
) -> dict[str, Any]:
    source = Path(source).resolve()
    if not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError("Exact commit required")
    release = destination(RELEASES / commit)
    if release.exists():
        raise ValueError("Existing release refused")
    files = source_files(source, commit)
    check_dependencies(source, files)
    node = Path(node_bin).resolve()
    if not node.is_file() or not os.access(node, os.X_OK):
        raise ValueError("Node executable missing")
    uid = pwd.getpwnam(owner).pw_uid
    if uid <= 0:
        raise ValueError("Non-root owner required")
    secret = str(telegram_file(telegram_env))
    channels: dict[str, Any] = {"test": {"bot": TEST_BOT}}
    wanted = {name: unit_for(name, secret) for name in UNITS}
    if main_env or main_bot_name:
        main_secret = str(telegram_file(main_env))
        if main_secret == secret:
            raise ValueError("Main and test environment files must differ")
        channels["main"] = {"bot": main_bot(main_bot_name)}
        wanted[MAIN_UNIT] = main_unit_text(main_secret)
    units = {}
    for name, text in wanted.items():
        target = destination(UNIT_DIR / name)
        if target.exists():
            stat = target.stat()
            if stat.st_uid != 0 or stat.st_mode & 0o022 or target.read_text() != text:
                raise ValueError("Existing unit differs")
        else:
            units[str(target)] = text
    destination(BASE / "current", allow_current=True)
    writes = [str(release / name) for name in files]
    writes += [str(release / name) for name in (".venv", ".install-tmp", PATH_FILE)]
    writes += [str(release / "madar-release.json")]
    writes += [str(DATA / name) for name in ("previews", "intents", "tmp")]
    writes += [str(STATUS_DATA / name) for name in ("previews", "runs", "tmp")]
    writes += [str(BASE), str(RELEASES), str(STATE), str(DATA), str(STATUS_DATA)]
    writes += [str(STATE / "render-work")]
    writes += list(units)
    for path in writes:
        destination(path)
    return {
        "source": str(source),
        "commit": commit,
        "release": str(release),
        "layout": LAYOUT,
        "files": files,
        "owner": owner,
        "owner_uid": uid,
        "node_bin": str(node),
        "units": units,
        "channels": channels,
        "writes": writes,
        "path_file": path_file(release),
        "commands": [
            PYTHON + " -m venv --without-pip <release>/.venv",
            "sudo -n -u <owner> env PATH=<node-directory>:/usr/bin:/bin"
            " <release>/.venv/bin/python -m snap_bot.check_env --require-node",
        ],
        "systemd_actions": [],
        "activates": False,
    }


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path: str | Path, value: bytes, mode: int = 0o644) -> None:
    path = destination(path)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    sync_dir(path.parent)


def activate(commit: str, dry_run: bool = False) -> dict[str, Any]:
    """Switch current to a verified release of either layout (cutover or rollback)."""
    if not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError("Exact commit required")
    release = destination(RELEASES / commit)
    manifest = release / "madar-release.json"
    destination(manifest)
    for item in (release, manifest):
        if not item.exists() or item.stat().st_uid != 0 or item.stat().st_mode & 0o022:
            raise ValueError("Untrusted installed release")
    if not manifest.is_file():
        raise ValueError("Untrusted installed release")
    value = json.loads(manifest.read_text())
    if value.get("commit") != commit or value.get("environment") != "PASS":
        raise ValueError("Release not verified")
    if not (release / ".venv/bin/python").is_file():
        raise ValueError("Release not verified")
    current = destination(BASE / "current", allow_current=True)
    previous = str(current.resolve()) if current.is_symlink() else None
    layout = value.get("layout", "lab")
    if dry_run:
        return {"target": str(release), "layout": layout, "previous": previous}
    temporary = destination(BASE / ("current." + uuid.uuid4().hex + ".new"))
    os.symlink(release, temporary)
    os.replace(temporary, current)
    sync_dir(BASE)
    return {"activated": commit, "layout": layout, "previous": previous}


def execute(plan: dict[str, Any]) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise ValueError("Installer requires root")
    source, release = Path(plan["source"]), destination(plan["release"])
    if release.exists():
        raise ValueError("Existing release refused")
    for directory in (BASE, RELEASES, STATE, DATA, STATUS_DATA):
        private = 0o711 if directory == STATE else 0o700
        mode = 0o755 if directory in (BASE, RELEASES) else private
        destination(directory).mkdir(parents=True, exist_ok=True, mode=mode)
        if directory.stat().st_uid != 0 or directory.stat().st_mode & 0o022:
            raise ValueError("Untrusted destination owner")
    destination(STATE / "render-work").mkdir(mode=0o711, exist_ok=True)
    release.mkdir(mode=0o755)
    for name in plan["files"]:
        path = destination(release / name)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Copy committed objects, never an unchecked working-tree snapshot.
        value = git(source, "show", plan["commit"] + ":" + name)
        write_new(path, value, 0o755 if (source / name).stat().st_mode & 0o111 else 0o644)
    temporary = destination(release / ".install-tmp")
    temporary.mkdir()
    env = {"PATH": "/usr/bin:/bin", "TMPDIR": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}
    venv = [PYTHON, "-m", "venv", "--without-pip", str(release / ".venv")]
    subprocess.run(venv, env=env, check=True)  # noqa: S603  # fixed argv
    if not (release / SITE).is_dir():
        raise ValueError("Unexpected venv layout")
    write_new(release / PATH_FILE, plan["path_file"].encode())
    check = ["sudo", "-n", "-u", plan["owner"], "env"]
    check += ["PATH=" + str(Path(plan["node_bin"]).parent) + ":/usr/bin:/bin"]
    check += ["PYTHONDONTWRITEBYTECODE=1", str(release / ".venv/bin/python")]
    check += ["-m", "snap_bot.check_env", "--require-node"]
    result = subprocess.run(  # noqa: S603  # fixed argv
        check, cwd=release, capture_output=True, text=True, check=True
    )
    if json.loads(result.stdout).get("runtime") != "PASS":
        raise ValueError("Environment check did not pass")
    manifest = {
        "commit": plan["commit"],
        "owner_uid": plan["owner_uid"],
        "layout": LAYOUT,
        "environment": "PASS",
        "channels": plan["channels"],
    }
    write_new(release / "madar-release.json", json.dumps(manifest).encode())
    for name in ("previews", "intents", "tmp"):
        destination(DATA / name).mkdir(mode=0o700, exist_ok=True)
    for name in ("previews", "runs", "tmp"):
        destination(STATUS_DATA / name).mkdir(mode=0o700, exist_ok=True)
    for target, text in plan["units"].items():
        write_new(Path(target), text.encode())
    return {"installed": plan["commit"], "activated": False}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--node-bin", default="/usr/bin/node")
    parser.add_argument("--telegram-env", default="")
    parser.add_argument("--telegram-main-env", default="")
    parser.add_argument("--telegram-main-bot", default="")
    parser.add_argument("--owner", default="ubuntu")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--activate-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.activate_only:
            if not args.dry_run and os.geteuid() != 0:
                raise ValueError("Root required")
            result = activate(args.commit, args.dry_run)
        else:
            if args.source is None:
                raise ValueError("Source required")
            plan = build_plan(
                args.source,
                args.commit,
                args.node_bin,
                args.telegram_env,
                args.owner,
                args.telegram_main_env,
                args.telegram_main_bot,
            )
            result = plan if args.dry_run else execute(plan)
        print(json.dumps(result, indent=2))
    except ValueError as error:
        # Guard messages are fixed strings written here; they never carry file contents.
        raise SystemExit("MADAR_INSTALL_BLOCKED: " + str(error)) from None
    except Exception:
        raise SystemExit("MADAR_INSTALL_BLOCKED") from None


if __name__ == "__main__":
    main()
