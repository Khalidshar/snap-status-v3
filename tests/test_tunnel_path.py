"""Portable cloudflared lookup without starting a tunnel or opening its token file."""

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "config/systemd/madar-v3-whatsapp-tunnel.service"


def launcher():
    line = next(line for line in UNIT.read_text().splitlines() if line.startswith("ExecStart="))
    return shlex.split(line.removeprefix("ExecStart="))


def test_tunnel_and_tools_resolve_the_binary_via_path():
    text = UNIT.read_text()
    assert "/usr/bin/cloudflared" not in text
    assert launcher()[:2] == ["/usr/bin/env", "cloudflared"]
    assert launcher()[2:] == [
        "tunnel",
        "--no-autoupdate",
        "run",
        "--token-file",
        "/etc/madar/whatsapp-v3-tunnel.token",
    ]
    tools = (ROOT / "scripts/ops/madar.sh").read_text().split("TOOLS=(", 1)[1].split(")", 1)[0]
    assert "/usr/bin/env" in tools.split() and "cloudflared" in tools.split()


@pytest.mark.skipif(sys.platform == "win32", reason="Linux service launcher")
def test_launcher_finds_a_binary_outside_usr_bin(tmp_path):
    binary = tmp_path / "cloudflared"
    binary.write_text("#!" + sys.executable + '\nprint("synthetic-cloudflared")\n')
    binary.chmod(0o755)
    result = subprocess.run(  # noqa: S603 - fixed env launcher and temporary fixture only
        [*launcher()[:2], "--version"],
        env={**os.environ, "PATH": str(tmp_path)},
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert result.stdout.strip() == "synthetic-cloudflared"
