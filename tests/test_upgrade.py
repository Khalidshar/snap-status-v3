"""Upgrade guards use temporary state and fake executables, never a host or credentials."""

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[1] / "scripts/ops"


def guard():
    spec = importlib.util.spec_from_file_location("upgrade_check", OPS / "upgrade_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def state(tmp_path):
    for name in ("snap/commands", "status/commands"):
        (tmp_path / name).mkdir(parents=True)
    return tmp_path


def ledger(state, *, expired=False, cancelled=False, sent=False):
    preview = "a" * 32
    video = "b" * 64
    events = [{"kind": "RENDERED", "video_sha": video, "preview_id": preview}]
    if sent:
        events.append({"kind": "ACK", "video_sha": video})
    previous = "0" * 64
    lines = []
    for seq, event in enumerate(events, 1):
        event.update(seq=seq, previous=previous, time=time.time())
        previous = hashlib.sha256(
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        lines.append(json.dumps({**event, "hash": previous}))
    (state / "snap/events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    folder = state / "snap/previews" / preview
    folder.mkdir(parents=True)
    (folder / "preview.json").write_text(
        json.dumps({"expires_at": time.time() + (-60 if expired else 600)})
    )
    (state / "snap/commands/pending.json").write_text(
        json.dumps({"cancelled": [video] if cancelled else []})
    )


def database(state, table, schema, values):
    file = state / "status/commands/jobs.sqlite"
    with sqlite3.connect(file) as db:
        db.execute(f"CREATE TABLE {table}({schema})")
        db.execute(f"INSERT INTO {table} VALUES({values})")
    return file


def test_live_lock_refuses_even_with_force_pending(state):
    file = database(state, "component_locks", "deadline REAL", str(time.time() + 600))
    before = file.read_bytes()
    for force in (False, True):
        with pytest.raises(ValueError, match="LIVE_LOCK"):
            guard().check(state, force_pending=force)
    assert file.read_bytes() == before


@pytest.mark.parametrize("bot", ["snap", "status"])
def test_pending_clip_requires_explicit_override_and_keeps_evidence(state, bot):
    if bot == "snap":
        ledger(state)
    else:
        database(
            state,
            "command_videos",
            "state TEXT, code TEXT",
            "'BLOCKED','TRANSLATION_REVIEW_REQUIRED'",
        )
    before = {p: p.read_bytes() for p in state.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="مقطع ينتظر موافقتك؛ النشر يلغيه"):
        guard().check(state)
    assert guard().check(state, force_pending=True)["pending_clips"] == 1
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("kind", ["expired", "cancelled", "sent"])
def test_old_decided_or_expired_snap_clip_does_not_block(state, kind):
    ledger(state, **{kind: True})
    assert guard().check(state)["pending_clips"] == 0


def test_corrupt_state_never_grants_permission(state):
    (state / "snap/events.jsonl").write_text('{"kind":"RENDERED"}')
    with pytest.raises(ValueError, match="LEDGER"):
        guard().check(state, force_pending=True)


def test_expired_lock_does_not_block_and_database_is_read_only(state):
    file = database(state, "component_locks", "deadline REAL", "0")
    before = file.read_bytes()
    assert guard().check(state) == {"live_locks": 0, "pending_clips": 0}
    assert file.read_bytes() == before


@pytest.fixture
def shell(tmp_path, state):
    if sys.platform == "win32" or not shutil.which("bash"):
        pytest.skip("Linux bash integration; pure guard tests run on Windows too")
    root = tmp_path / "fake-root"
    root.mkdir()
    base = tmp_path / "madar"
    old = base / "releases" / ("c" * 40)
    old.mkdir(parents=True)
    (base / "current").symlink_to(old)
    script = tmp_path / "upgrade.sh"
    script.write_text(
        (OPS / "upgrade.sh").read_text()
        .replace("/opt/madar", str(base))
        .replace("/root/v3-", str(root / "v3-"))
    )
    shutil.copyfile(OPS / "upgrade_check.py", tmp_path / "upgrade_check.py")
    config = tmp_path / "config.toml"
    config.write_text("[runtime]\nstate_root = " + json.dumps(str(state)))
    log = tmp_path / "calls.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = '''import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\\n")
if name == "id":
    print("0")
elif name == "systemctl":
    if args[0] == "list-units" and "--failed" not in args:
        if os.environ.get("FAKE_BUSY") == "1": print("madar-v3-snap-render@style.service active")
    elif args[0] == "is-active": print("active")
elif name == "git":
    if args[0] == "clone":
        folder = pathlib.Path(args[-1]) / "apps/snap-bot/snap_bot"
        folder.mkdir(parents=True)
        (folder / "release.py").write_text(os.environ["FAKE_RELEASE"])
elif name == "release.py" and "--activate-only" in args:
    current = pathlib.Path(os.environ["FAKE_BASE"]) / "current"
    current.unlink()
    current.symlink_to(current.parent / "releases" / args[args.index("--commit") + 1])
'''
    for name in ("id", "systemctl", "git", "sleep"):
        path = bin_dir / name
        path.write_text("#!" + sys.executable + "\n" + fake)
        path.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "STUDIO_CONFIG": str(config),
        "FAKE_BASE": str(base),
        "FAKE_RELEASE": fake,
        "FAKE_LOG": str(log),
    }

    def run(*args):
        result = subprocess.run(  # noqa: S603 - fixed repo script, isolated fake executables
            ["bash", str(script), "d" * 40, *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls

    return run, env


def test_upgrade_bash_syntax():
    if sys.platform == "win32" or not shutil.which("bash"):
        pytest.skip("Linux bash required")
    subprocess.run(["bash", "-n", str(OPS / "upgrade.sh")], check=True)  # noqa: S603


def test_busy_service_blocks_before_clone_or_release(shell):
    run, env = shell
    env["FAKE_BUSY"] = "1"
    result, calls = run("--force-pending")
    assert result.returncode != 0 and "clip is being built" in result.stdout
    assert not any(call[0] in {"git", "release.py"} for call in calls)


@pytest.mark.parametrize("kind", ["lock", "snap", "status"])
def test_shell_refuses_live_lock_or_pending_clip_before_release(shell, state, kind):
    if kind == "lock":
        database(state, "component_locks", "deadline REAL", str(time.time() + 600))
    elif kind == "snap":
        ledger(state)
    else:
        database(
            state, "command_videos", "state TEXT,code TEXT",
            "'BLOCKED','TRANSLATION_REVIEW_REQUIRED'",
        )
    result, calls = shell[0]()
    assert result.returncode != 0
    assert not any(call[0] in {"git", "release.py"} for call in calls)


def test_install_then_activate_exact_commit_and_restart_ten_services(shell, state):
    ledger(state)
    result, calls = shell[0]("--force-pending")
    assert result.returncode == 0, result.stdout + result.stderr
    releases = [call for call in calls if call[0] == "release.py"]
    assert len(releases) == 2 and "--source" in releases[0]
    assert releases[1] == ["release.py", "--activate-only", "--commit", "d" * 40]
    first = releases[0]
    for flag, value in {
        "--commit": "d" * 40,
        "--owner": "khalid",
        "--telegram-env": "/etc/studio-v2/snap-telegram-test.env",
        "--telegram-main-env": "/etc/madar/snap-telegram-main.env",
        "--telegram-main-bot": "khalid_s2ss_bot",
    }.items():
        assert first[first.index(flag) + 1] == value
    restarts = [call for call in calls if call[:2] == ["systemctl", "restart"]]
    assert len(restarts) == 1 and len(restarts[0][2:]) == 10
    assert "OLD=" in result.stdout and "CURRENT=" in result.stdout and "FAILED=0" in result.stdout
    assert "--activate-only --commit " + "c" * 40 in result.stdout
