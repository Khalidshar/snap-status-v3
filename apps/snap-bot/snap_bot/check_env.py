"""Fail-fast check that this host matches the pinned render/isolation runtime.

Ported from the lab (snap-bot-v2 tools/check_env.py). The release installer runs it
as the non-root owner before a release can be activated.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
PROFILES = ("config/snap/did_you_know.toml", "config/snap/weather.toml")
UBUNTU = "24.04"
PYTHON = (3, 12)
NODE_MAJOR = 22


def run(args: list[str]) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603  # fixed argv, no shell
            args, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    try:
        text = path.read_text()
    except OSError:
        return {}
    pairs = (line.partition("=") for line in text.splitlines())
    return {key: value.strip('"') for key, _, value in pairs if key}


def profiles(root: Path = ROOT) -> list[dict[str, Any]]:
    return [
        tomllib.loads((root / name).read_text(encoding="utf-8"))["slice"]["profile"]
        for name in PROFILES
    ]


def probe() -> dict[str, Any]:
    init = Path("/proc/1/comm")
    return {
        "os": os_release(),
        "python": sys.version_info[:2],
        "euid": os.geteuid(),
        "init": init.read_text().strip() if init.exists() else "",
        "sudo": run(["sudo", "-n", "true"]) is not None,
        "ffmpeg": (run(["/usr/bin/ffmpeg", "-version"]) or "").splitlines()[:1],
        "tools": {tool: Path(tool).is_file() for tool in ("/usr/bin/ffprobe", "/usr/bin/fc-query")},
        "systemd_run": Path("/usr/bin/systemd-run").is_file(),
        "node": (run([shutil.which("node") or "node", "--version"]) or "").strip(),
    }


def font_problems(profile: dict[str, Any], root: Path = ROOT) -> list[str]:
    problems = []
    for name, expected in profile.get("font_files", {}).items():
        if name.startswith("system:"):
            continue
        path = root / "assets/fonts" / name
        if not path.is_file():
            problems.append("missing font " + name)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            problems.append("font hash mismatch " + name)
    return problems


def problems(
    facts: dict[str, Any],
    pinned: list[dict[str, Any]],
    *,
    require_node: bool = False,
    require_isolation: bool = True,
    root: Path = ROOT,
) -> list[str]:
    found = []
    release = facts["os"]
    if release.get("ID") != "ubuntu" or release.get("VERSION_ID") != UBUNTU:
        found.append(
            "Ubuntu " + UBUNTU + " required, found " + (release.get("PRETTY_NAME") or "unknown")
        )
    if tuple(facts["python"]) != PYTHON:
        found.append("Python {}.{} required, found {}.{}".format(*PYTHON, *facts["python"]))
    versions = {profile["ffmpeg_version"] for profile in pinned}
    actual = (facts["ffmpeg"] or ["missing"])[0]
    if actual not in versions:
        found.append("FFmpeg must be " + " / ".join(sorted(versions)) + "; found " + actual)
    found += ["missing " + tool for tool, ok in facts["tools"].items() if not ok]
    for profile in pinned:
        for problem in font_problems(profile, root):
            if problem not in found:
                found.append(problem)
    if require_isolation:
        if facts["init"] != "systemd":
            found.append("systemd must be PID 1 for renderer isolation")
        if not facts["systemd_run"]:
            found.append("missing /usr/bin/systemd-run")
        if facts["euid"] == 0:
            found.append("run as a non-root user; isolation tests rely on UID separation")
        if not facts["sudo"]:
            found.append("passwordless sudo is required for disposable systemd-run units")
    if require_node:
        match = re.fullmatch(r"v(\d+)\..*", facts["node"])
        if not match or int(match.group(1)) != NODE_MAJOR:
            found.append(f"Node {NODE_MAJOR} required, found {facts['node'] or 'missing'}")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-node", action="store_true", help="also require Node 22 (agent runtime)"
    )
    parser.add_argument(
        "--no-isolation", action="store_true", help="skip systemd/sudo/UID checks (render-only)"
    )
    args = parser.parse_args(argv)
    found = problems(
        probe(),
        profiles(),
        require_node=args.require_node,
        require_isolation=not args.no_isolation,
    )
    result = {"runtime": "PASS" if not found else "FAIL", "problems": found}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
