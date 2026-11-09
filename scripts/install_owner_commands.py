"""Install private owner-command services from the active immutable release.

Default is a plan. --apply installs files only; starting services is separate.
No public listener, TLS, firewall, token creation or Meta subscription changes.
"""

import argparse
import hashlib
import json
import os
import pwd
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNITS = Path("/etc/systemd/system")
BACKUPS = Path("/var/backups/madar-owner-command-units")
WA_ENV = "/etc/madar/status-whatsapp.env"
MEDIA_ENV = "/etc/madar/media.env"
VIDEO_SOCKET = "/run/madar/snap-video.sock"
# Node 22 + Playwright + headless Chromium for the scene renderer (docs/DEPLOY-V3.md).
NODE_RUNTIME = Path("/opt/madar/node-runtime")


def service(
    app: str, description: str, command: str, *, network: bool, secret: bool = False
) -> str:
    return f"""[Unit]
Description={description}
After=network-online.target
[Service]
Type=simple
User=root
WorkingDirectory=/opt/madar/current
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
CapabilityBoundingSet=
ReadWritePaths=/var/lib/madar/{app}
RestrictAddressFamilies={"AF_UNIX AF_INET AF_INET6" if network else "AF_UNIX"}
{"EnvironmentFile=" + WA_ENV if secret else ""}
ExecStart=/opt/madar/current/.venv/bin/python {command}
Restart=on-failure
RestartSec=5
MemoryMax=256M
[Install]
WantedBy=multi-user.target
"""


def video_unit(app: str = "snap") -> str:
    """Root-only video RPC; secrets belong solely to this network process."""
    unit = service(
        "snap",
        "Madar isolated Snap video gateway",
        "-m studio_core.network.video --socket "
        + VIDEO_SOCKET
        + " --source-policy /opt/madar/current/config/media/source-video.json",
        network=True,
    )
    unit = unit.replace(
        "ReadWritePaths=/var/lib/madar/snap",
        "ReadWritePaths=/run/madar\n"
        "RuntimeDirectory=madar\n"
        "RuntimeDirectoryMode=0750\n"
        "Group=ubuntu\n"
        "RuntimeDirectoryPreserve=yes\n"
        "EnvironmentFile=" + MEDIA_ENV,
    )
    # Two simultaneous bounded 50-MiB transfers plus base64/JSON transport copies.
    unit = unit.replace("MemoryMax=256M", "MemoryMax=1G\nTasksMax=16\nLimitNOFILE=128")
    # Installation must not advertise boot enablement or automatic retry.
    unit = unit.replace("Restart=on-failure\nRestartSec=5", "Restart=no")
    return unit.split("[Install]", 1)[0].replace("snap-video.sock", app + "-video.sock")


def owner() -> str:
    """The non-root account the release was installed for (release.py --owner)."""
    manifest = ROOT / "madar-release.json"
    if manifest.is_file():
        # The manifest pins the owner's uid (release.py); its account name follows from it.
        uid = json.loads(manifest.read_text()).get("owner_uid")
        if type(uid) is int and uid > 0:
            return pwd.getpwuid(uid).pw_name
    return "ubuntu"


def plan() -> dict[Path, str]:
    files = {UNITS / f"madar-v3-{app}-video.service": video_unit(app) for app in ("snap", "status")}
    for app in ("snap", "status"):
        files[UNITS / f"madar-v3-{app}-news-egress.service"] = service(
            app,
            f"Madar {app} written-news Network Core proxy",
            f"-m studio_core.cli --config /opt/madar/current/config/{app}/news.toml proxy",
            network=True,
        )
    files[UNITS / "madar-v3-status-commands.service"] = service(
        "status",
        "Madar owner-only WhatsApp command consumer",
        "-m status_bot.owner_commands --config /opt/madar/current/config/status/commands.toml "
        "--news-config /opt/madar/current/config/status/news.toml",
        network=True,
        secret=True,
    )
    files[UNITS / "madar-v3-status-command-webhook.service"] = service(
        "status",
        "Madar authenticated WhatsApp command webhook (private socket)",
        "-m status_bot.review_service --mode webhook "
        "--config /opt/madar/current/config/status/commands.toml "
        "--settings /etc/madar/status-command-webhook.json "
        "--socket /var/lib/madar/status/command-webhook/incoming.sock "
        "--proxy-uid 0 --enable-listener",
        network=False,
        secret=True,
    )
    # A crashed listener leaves its socket behind; the next start must not refuse it.
    files[UNITS / "madar-v3-status-command-webhook.service.d/stale-socket.conf"] = (
        "[Service]\nExecStartPre=/bin/rm -f /var/lib/madar/status/command-webhook/incoming.sock\n"
    )
    # The WhatsApp clip (sources, background footage, ffmpeg) is built inside this service;
    # 256M left it swapping 1.7G and crawling. Same budget as the Snap clip renderer.
    files[UNITS / "madar-v3-status-commands.service.d/memory.conf"] = "[Service]\nMemoryMax=3G\n"
    # A production clip (footage, Quran audio, Chromium scenes, ffmpeg) needs about 8 CPU
    # minutes; the release's 300 s start limit killed it mid-render. Same as Quran renders.
    for name in ("madar-v3-snap-render@.service", "madar-v3-snap-render.service"):
        files[UNITS / (name + ".d/timeout.conf")] = "[Service]\nTimeoutStartSec=1200\n"
    files[UNITS / "madar-v3-snap-commands.service.d/live-news.conf"] = (
        "[Service]\nEnvironment=MADAR_LIVE_NEWS=1\n"
    )
    unit = (
        service(
            "snap",
            "Madar isolated live-news clip renderer",
            "-m snap_bot.news_clips %i",
            network=False,
        )
        .replace("Type=simple", "Type=oneshot")
        .replace("Restart=on-failure", "Restart=no")
    )
    unit = unit.replace(
        "MemoryMax=256M",
        "MemoryMax=1536M\nTimeoutStartSec=300\nPrivateNetwork=yes\nInaccessiblePaths=-/run/madar",
    )
    files[UNITS / "madar-v3-snap-news-clip@.service"] = unit
    templates = ROOT / "config/systemd"
    for name in (
        # Each WhatsApp clip and each requested Snap brief runs in one of these.
        "madar-v3-status-content@.service",
        "madar-v3-snap-brief@.service",
        "madar-v3-quran-egress@.service",
        "madar-v3-production-egress@.service",
        "madar-v3-quran-retention@.service",
        "madar-v3-snap-quran-prepare@.service",
        "madar-v3-status-quran-prepare@.service",
        "madar-v3-snap-quran-render@.service",
        "madar-v3-status-quran-render@.service",
        # Owner decision 9 Nov 2026: three scheduled clips a day in both bots.
        "madar-v3-schedule@.service",
        "madar-v3-schedule-morning.timer",
        "madar-v3-schedule-noon.timer",
        "madar-v3-schedule-night.timer",
    ):
        files[UNITS / name] = (templates / name).read_text()
    files[UNITS / "madar-v3-status-content@.service.d/quran.conf"] = (
        "[Service]\nReadWritePaths=-/var/lib/madar/quran-status\n"
    )
    # The WhatsApp clip is prepared in the command consumer; its Quran Foundation reservations
    # live in quran-status, which strict sandboxing left read-only, so every WhatsApp clip fell
    # back to EveryAyah without word timings (owner 8 Nov 2026: «تلاوة» in both bots).
    files[UNITS / "madar-v3-status-commands.service.d/quran-store.conf"] = (
        "[Service]\nReadWritePaths=-/var/lib/madar/quran-status\n"
    )
    for name in (
        "madar-v3-snap-render@.service",
        "madar-v3-snap-render.service",
        "madar-v3-status-commands.service",
    ):
        files[UNITS / (name + ".d/quran.conf")] = "[Service]\nEnvironment=MADAR_QURAN_AUDIO=0\n"
    for name in (
        "madar-v3-snap-render@.service",
        "madar-v3-snap-render.service",
        "madar-v3-status-commands.service",
    ):
        files[UNITS / (name + ".d/production.conf")] = (
            "[Service]\nEnvironment=MADAR_LEGACY_PRODUCTION=0\n"
        )
    # The WhatsApp clip itself renders in status-content@, so it needs the same switches;
    # the owner's zz-*.conf overrides (DEPLOY-V3.md) turn them on for every one of these.
    files[UNITS / "madar-v3-status-content@.service.d/production.conf"] = (
        "[Service]\nEnvironment=MADAR_LEGACY_PRODUCTION=0\nEnvironment=MADAR_QURAN_AUDIO=0\n"
    )
    browsers = sorted(
        NODE_RUNTIME.glob("browsers/chromium_headless_shell-*/*/chrome-headless-shell")
    )
    if (NODE_RUNTIME / "bin/node").is_file() and browsers:
        runtime = (
            "[Service]\n"
            f"Environment=MADAR_NODE={NODE_RUNTIME}/bin/node\n"
            f"Environment=NODE_PATH={NODE_RUNTIME}/node_modules\n"
            f"Environment=MADAR_CHROMIUM={browsers[-1]}\n"
        )
        for name in (
            "madar-v3-snap-render@.service",
            "madar-v3-snap-render.service",
            "madar-v3-status-commands.service",
            "madar-v3-status-content@.service",
        ):
            files[UNITS / (name + ".d/node-runtime.conf")] = runtime
    name = owner()
    if name != "ubuntu":
        # A machine whose non-root account is not "ubuntu" (Khalid's local WSL server).
        files = {
            path: text.replace("User=ubuntu\n", f"User={name}\n").replace(
                "Group=ubuntu\n", f"Group={name}\n"
            )
            for path, text in files.items()
        }
    return files


def read_unit(path: Path) -> bytes | None:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Symlink unit refused")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"Non-file unit refused: {path}")
    return path.read_bytes()


def unit_changes(files: dict[Path, str]) -> dict[Path, bytes | None]:
    """Validate every target before the first write and keep the approved bytes."""
    # The manifest maps each relative unit path to exact SHA-256 hashes and their
    # source commits. When changing a unit, record its previous released bytes here.
    history = json.loads((ROOT / "config/owner-command-unit-history.json").read_text())
    changes = {}
    for path, value in files.items():
        old = read_unit(path)
        if old == value.encode():
            continue
        if old is not None:
            known = history.get(path.relative_to(UNITS).as_posix(), {})
            if hashlib.sha256(old).hexdigest() not in known:
                raise ValueError(f"Existing different unit refused: {path}")
        changes[path] = old
    return changes


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def backup_units(changes: dict[Path, bytes | None]) -> Path | None:
    if all(old is None for old in changes.values()):
        return None
    if any(p.is_symlink() for p in (BACKUPS, *BACKUPS.parents)):
        raise ValueError("Symlink backup directory refused")
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    info = BACKUPS.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("Untrusted backup directory")
    directory = Path(tempfile.mkdtemp(prefix="install-", dir=BACKUPS))
    for path, old in changes.items():
        if old is None:
            continue
        backup = directory / path.relative_to(UNITS)
        backup.parent.mkdir(mode=0o700, exist_ok=True, parents=True)
        with backup.open("xb") as stream:
            stream.write(old)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(backup.parent)
    sync_directory(directory)
    sync_directory(BACKUPS)
    sync_directory(BACKUPS.parent)
    return directory


def install_unit(path: Path, value: bytes, old: bytes | None) -> None:
    path.parent.mkdir(mode=0o755, exist_ok=True, parents=True)
    fd, filename = tempfile.mkstemp(dir=path.parent)
    staged = Path(filename)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        current = read_unit(path)
        if current == value:
            return
        if current != old:
            raise ValueError(f"Unit changed during install: {path}")
        if old is None:
            os.link(staged, path)
        else:
            os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)
    sync_directory(path.parent)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    files = plan()
    backup = None
    if args.apply:
        manifest = json.loads((ROOT / "madar-release.json").read_text())
        if (
            os.geteuid() != 0
            or Path("/opt/madar/current").resolve() != ROOT
            or not re.fullmatch("[0-9a-f]{40}", manifest["commit"])
        ):
            raise ValueError("Active installed release required")
        # #72 must be present in the active release before installing its launcher.
        # Never open media.env: systemd loads it only when an operator starts the unit.
        if not (ROOT / "packages/studio-core/studio_core/network/video.py").is_file():
            raise ValueError("Video gateway module missing; merge and install #72 first")
        changes = unit_changes(files)
        for app in ("snap", "status"):
            path = Path(f"/var/lib/madar/{app}/news")
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError("Unsafe command state")
        for name in ("snap/news", "status/news", "status/commands", "status/command-webhook"):
            Path("/var/lib/madar", name).mkdir(mode=0o700, exist_ok=True, parents=True)
        account = pwd.getpwnam(owner())
        for app in ("snap", "status"):
            root = Path("/var/lib/madar/quran-" + app)
            if any(p.is_symlink() for p in (root, *root.parents)):
                raise ValueError("Unsafe Quran state")
            root.mkdir(mode=0o2770, parents=True, exist_ok=True)
            # Same rule as the production folders below: status services run as root without
            # CAP_DAC_OVERRIDE, so an owner-held quran-status refused them and every WhatsApp
            # Quran clip silently fell back to EveryAyah (8 Nov 2026). Snap stays the owner's.
            uid, gid = (account.pw_uid, account.pw_gid) if app == "snap" else (0, 0)
            for path in (root, *root.rglob("*")):
                if not path.is_symlink():
                    os.chown(path, uid, gid)
            root.chmod(0o2770)
        for app in ("snap", "status"):
            production = Path("/var/lib/madar") / app / "production"
            if any(p.is_symlink() for p in (production, *production.parents)):
                raise ValueError("Unsafe production state")
            production.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Only the Snap renderer is non-root. Status services run as root without
            # CAP_DAC_OVERRIDE, so an ubuntu-owned status folder refuses them (PermissionError).
            if app == "snap":
                os.chown(production, account.pw_uid, account.pw_gid)
            else:
                os.chown(production, 0, 0)
            production.chmod(0o700)
            # The non-root renderer must pass through the root-owned app state to reach its
            # own production folder: traverse only, no listing (files inside stay 0600/0700).
            production.parent.chmod(0o711)
        backup = backup_units(changes)
        for path, old in changes.items():
            install_unit(path, files[path].encode(), old)
    print(
        json.dumps(
            {
                "applied": args.apply,
                "files": [str(p) for p in files],
                "backup": str(backup) if backup else None,
                "started_services": [],
                "public_listener_created": False,
            }
        )
    )


if __name__ == "__main__":
    main()
