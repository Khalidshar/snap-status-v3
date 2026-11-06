"""Install private owner-command services from the active immutable release.

Default is a plan. --apply installs files only; starting services is separate.
No public listener, TLS, firewall, token creation or Meta subscription changes.
"""

import argparse
import json
import os
import pwd
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNITS = Path("/etc/systemd/system")
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
    files[UNITS / "madar-v3-status-commands.service.d/memory.conf"] = "[Service]\nMemoryMax=1536M\n"
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
    ):
        files[UNITS / name] = (templates / name).read_text()
    files[UNITS / "madar-v3-status-content@.service.d/quran.conf"] = (
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
    return files


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    files = plan()
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
        # Validate every target before the first write; never replace a different unit.
        for path, value in files.items():
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError("Symlink unit refused")
            if path.exists() and path.read_text() != value:
                raise ValueError("Existing different unit refused")
        for app in ("snap", "status"):
            path = Path(f"/var/lib/madar/{app}/news")
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError("Unsafe command state")
        for name in ("snap/news", "status/news", "status/commands", "status/command-webhook"):
            Path("/var/lib/madar", name).mkdir(mode=0o700, exist_ok=True, parents=True)
        account = pwd.getpwnam("ubuntu")
        for app in ("snap", "status"):
            root = Path("/var/lib/madar/quran-" + app)
            if any(p.is_symlink() for p in (root, *root.parents)):
                raise ValueError("Unsafe Quran state")
            root.mkdir(mode=0o2770, parents=True, exist_ok=True)
            os.chown(root, account.pw_uid, account.pw_gid)
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
        for path, value in files.items():
            path.parent.mkdir(mode=0o755, exist_ok=True, parents=True)
            if not path.exists():
                with path.open("x") as stream:
                    stream.write(value)
    print(
        json.dumps(
            {
                "applied": args.apply,
                "files": [str(p) for p in files],
                "started_services": [],
                "public_listener_created": False,
            }
        )
    )


if __name__ == "__main__":
    main()
