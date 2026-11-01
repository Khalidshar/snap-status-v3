"""Install private owner-command services from the active immutable release.

Default is a plan. --apply installs files only; starting services is separate.
No public listener, TLS, firewall, token creation or Meta subscription changes.
"""

import argparse
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNITS = Path("/etc/systemd/system")
WA_ENV = "/etc/madar/status-whatsapp.env"
MEDIA_ENV = "/etc/madar/media.env"
VIDEO_SOCKET = "/run/madar/snap-video.sock"


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


def video_unit() -> str:
    """Root-only video RPC; secrets belong solely to this network process."""
    unit = service(
        "snap",
        "Madar isolated Snap video gateway",
        "-m studio_core.network.video --socket " + VIDEO_SOCKET,
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
    return unit.split("[Install]", 1)[0]


def plan() -> dict[Path, str]:
    files = {UNITS / "madar-v3-snap-video.service": video_unit()}
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
        "MemoryMax=256M", "MemoryMax=1536M\nTimeoutStartSec=300\nPrivateNetwork=yes"
    )
    files[UNITS / "madar-v3-snap-news-clip@.service"] = unit
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
