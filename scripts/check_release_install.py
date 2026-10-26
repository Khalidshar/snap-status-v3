"""Disposable real install of a v3 Snap release, then cutover and rollback. No service runs.

Every installer root is redirected into one temporary directory; the only file outside it
is a dummy Telegram environment file under /etc, removed afterwards. The real check_env
runs as the non-root owner, so the runner must match the pinned host (Ubuntu 24.04,
Python 3.12, pinned FFmpeg, Node 22, systemd).
Usage (disposable Linux root with systemd as PID 1, e.g. a CI runner):
  sudo -n /usr/bin/python3 scripts/check_release_install.py --node-bin "$(command -v node)"
"""

import argparse
import importlib.util
import json
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_installer() -> Any:
    # Loaded by path with the system Python, exactly as on the server (no venv yet).
    spec = importlib.util.spec_from_file_location(
        "madar_release", ROOT / "apps/snap-bot/snap_bot/release.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Installer missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(args: list[str], timeout: int = 120, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # fixed argv, no shell
        args, capture_output=True, text=True, timeout=timeout, **kwargs
    )


def refused(call: Any, *args: Any) -> bool:
    try:
        call(*args)
    except ValueError:
        return True
    return False


def redirect(installer: Any, root: Path) -> None:
    installer.BASE = root / "opt/madar"
    installer.RELEASES = installer.BASE / "releases"
    installer.STATE = root / "var/lib/madar"
    installer.DATA = installer.STATE / "snap"
    installer.STATUS_DATA = installer.STATE / "status"
    installer.UNIT_DIR = root / "etc/systemd/system"
    installer.UNIT_DIR.mkdir(parents=True)


def fake_lab_release(installer: Any, commit: str, uid: int) -> Path:
    release = installer.RELEASES / commit
    (release / ".venv/bin").mkdir(parents=True, mode=0o755)
    os.symlink(sys.executable, release / ".venv/bin/python")
    manifest = {"commit": commit, "owner_uid": uid, "environment": "PASS"}
    (release / "madar-release.json").write_text(json.dumps(manifest))
    return release


def status_render(release: Path, state: Path) -> dict[str, Any]:
    """Run the installed Status controller for real: live weather, no-network render."""
    python = str(release / ".venv/bin/python")
    command = [python, "-I", "-m", "status_bot.server_preview", "render", "--state", str(state)]
    result = run(command, cwd=release, env={"PATH": "/usr/bin:/bin"}, timeout=600)
    if result.returncode:
        raise RuntimeError("Status render failed: " + result.stderr[-3000:])
    value: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    preview = state / "previews" / value["preview_id"]
    ledger = (state / "events.jsonl").read_text().splitlines()
    if (
        value["qa"]["result"] != "PASS"
        or not all(value["network_isolation"].values())
        or value["delivery"] != "NOT_ATTEMPTED"
        or value["source"] == "synthetic-fixture"
        or len(ledger) != 1
        or json.loads(ledger[0])["video_sha"] != value["video_sha"]
        or sorted(path.name for path in preview.iterdir())
        != [
            "evidence.json",
            "lease.json",
            "manifest.json",
            "preview.png",
            "render-result.json",
            "weather.mp4",
        ]
        or (preview / "lease.json").stat().st_mode & 0o777 != 0o600
    ):
        raise RuntimeError("Unexpected Status preview: " + json.dumps(value, ensure_ascii=False))
    units = run(["systemctl", "list-units", "--all", "--no-legend", "madar-v3-status-*"])
    if units.stdout.strip():
        raise RuntimeError("Transient Status units left behind: " + units.stdout)
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node-bin", required=True)
    parser.add_argument("--status-render", action="store_true")
    options = parser.parse_args()
    if os.geteuid() != 0 or Path("/proc/1/comm").read_text().strip() != "systemd":
        raise SystemExit("Disposable Linux root/systemd required")
    owner = os.environ.get("SUDO_USER", "")
    if not owner or pwd.getpwnam(owner).pw_uid == 0:
        raise SystemExit("Run through sudo from a non-root account")
    installer = load_installer()
    root = Path(tempfile.mkdtemp(prefix="madar-release-check-", dir="/var/lib"))
    root.chmod(0o755)
    secret = Path("/etc/madar-release-check-" + secrets.token_hex(4) + ".env")
    try:
        redirect(installer, root)
        source = root / "source"
        commit = run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True).stdout.strip()
        run(["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(source)], check=True)
        run(["git", "-C", str(source), "checkout", "-q", commit], check=True)
        fd = os.open(secret, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write("TG_TEST_TOKEN=fixture-only\nTG_TEST_CHAT=0\n")

        plan = installer.build_plan(source, commit, options.node_bin, str(secret), owner)
        if plan["activates"] or plan["systemd_actions"]:
            raise RuntimeError("Plan must not activate or touch systemd")
        result = installer.execute(plan)
        release = installer.RELEASES / commit
        if result != {"installed": commit, "activated": False}:
            raise RuntimeError("Unexpected install result")
        if (installer.BASE / "current").exists():
            raise RuntimeError("Install must not activate")
        manifest = json.loads((release / "madar-release.json").read_text())
        if manifest["layout"] != "v3" or manifest["environment"] != "PASS":
            raise RuntimeError("Unverified manifest")
        units = sorted(path.name for path in installer.UNIT_DIR.iterdir())
        if units != sorted(installer.UNITS):
            raise RuntimeError("Unexpected units")

        # The owner's renderer imports the release's own packages, from the release only.
        python = str(release / ".venv/bin/python")
        probe = (
            "import snap_bot.actions_preview as a, status_bot.server_preview as t, studio_core as c"
            "; print(a.ROOT, t.ROOT, c.__file__)"
        )
        account = ["sudo", "-n", "-u", owner, "env", "PATH=/usr/bin:/bin"]
        imported = run([*account, python, "-I", "-c", probe], cwd="/", check=True).stdout.split()
        roots = {Path(imported[0]), Path(imported[1])}
        if roots != {release} or release not in Path(imported[2]).parents:
            raise RuntimeError("Release imports leak outside the release: " + " ".join(imported))
        # A lab unit pointed at a v3 release must fail before doing anything.
        lab = run(
            [python, "-m", "snap_app.server_preview", "render"],
            cwd=release,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(release / "apps/snap-bot")},
        )
        if lab.returncode == 0 or "No module named 'snap_app'" not in lab.stderr:
            raise RuntimeError("Lab unit did not fail closed on a v3 release")

        # Sabotage: an existing release is immutable and a changed unit is refused.
        if not refused(installer.build_plan, source, commit, options.node_bin, str(secret), owner):
            raise RuntimeError("Existing release was not refused")
        unit = installer.UNIT_DIR / installer.UNITS[0]
        unit.write_text(unit.read_text() + "ExecStartPost=/bin/true\n")
        (installer.RELEASES / commit).rename(root / "moved")
        if not refused(installer.build_plan, source, commit, options.node_bin, str(secret), owner):
            raise RuntimeError("Changed unit was not refused")
        (root / "moved").rename(installer.RELEASES / commit)

        # Cutover to v3, rollback to a lab release, then forward again.
        lab_commit = "0" * 40
        fake_lab_release(installer, lab_commit, pwd.getpwnam(owner).pw_uid)
        steps = [
            installer.activate(commit),
            installer.activate(lab_commit, True),
            installer.activate(lab_commit),
            installer.activate(commit),
        ]
        if (installer.BASE / "current").resolve() != release:
            raise RuntimeError("Cutover did not land on the v3 release")
        if [step.get("layout") for step in steps] != ["v3", "lab", "lab", "v3"]:
            raise RuntimeError("Unexpected activation layouts")
        if steps[2]["previous"] != str(release) or steps[3]["previous"] != str(
            installer.RELEASES / lab_commit
        ):
            raise RuntimeError("Activation history mismatch")
        status = None
        if options.status_render:
            status = status_render(release, installer.STATUS_DATA)
        print(
            json.dumps(
                {
                    "passed": True,
                    "commit": commit,
                    "environment": manifest["environment"],
                    "units": units,
                    "systemd_actions": [],
                    "rollback_to_lab": True,
                    "lab_unit_fails_closed": True,
                    "status_preview": status
                    and {key: status[key] for key in ("source", "video_sha", "qa", "delivery")},
                }
            )
        )
    finally:
        secret.unlink(missing_ok=True)
        resolved = root.resolve()
        if resolved.parent != Path("/var/lib") or not resolved.name.startswith(
            "madar-release-check-"
        ):
            raise RuntimeError("Unsafe fixture cleanup")
        shutil.rmtree(resolved)
        print("RELEASE_INSTALL_FIXTURE_CLEANUP_VERIFIED")


if __name__ == "__main__":
    main()
