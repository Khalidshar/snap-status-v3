"""Cutover safety using only temporary data and fake transports; no real hosts/state."""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[1] / "scripts/ops"


def load(name):
    spec = importlib.util.spec_from_file_location(name, OPS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


host = load("switch_host")
control = load("switch_control")


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "state"
    (root / "snap").mkdir(parents=True)
    (root / "status").mkdir()
    (root / "snap/offset.json").write_text('{"offset":12}')
    (root / "snap/video.mp4").write_bytes(b"synthetic video")
    return root


class Fake:
    def __init__(self, fail=None):
        self.events = []
        self.fail = fail
        self.data = {"snap/offset.json": [384, 0, 0, 2, "sha"]}

    def call(self, side, op, payload):
        self.events.append((side, op, payload))
        if (side, op) == self.fail:
            raise ValueError("synthetic failure")
        if op == "inspect":
            return {
                "identity": side,
                "commit": "a" * 40,
                "key_sizes": [10] * 8,
                "units": ["snap", "status"],
                "states": {
                    "snap": "active" if side == "source" else "inactive",
                    "status": "active" if side == "source" else "inactive",
                },
            }
        if op == "manifest":
            return self.data.copy()
        return {}

    def copy(self, source, identifier, names):
        self.events.append((source, "copy", names))
        if self.fail == (source, "copy"):
            raise ValueError("copy failed")


def test_dry_run_never_constructs_transport_or_reads_state(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["switch_control.py"])
    monkeypatch.setattr(control, "Transport", lambda *a: pytest.fail("network forbidden"))
    monkeypatch.setattr(control.os, "geteuid", lambda: pytest.fail("even root check unnecessary"))
    assert control.main() == 0
    assert "تجربة جافة" in capsys.readouterr().out


def test_both_bots_keys_size_only_and_inventory():
    services, keys, tools = host.units(OPS.parent.parent)
    assert len(services) == 14 and len(keys) == 8
    assert "madar-v3-snap-commands.service" in services
    assert "madar-v3-status-commands.service" in services
    assert "ffmpeg" in tools


@pytest.mark.parametrize(
    "table, schema, row",
    [
        ("component_locks", "deadline REAL", "0"),
        ("jobs", "state TEXT", "'RENDERING'"),
        ("jobs", "state TEXT", "'QUEUED'"),
        ("command_videos", "state TEXT", "'SEND_INTENT'"),
        ("panel_actions", "state TEXT", "'UNKNOWN'"),
    ],
)
def test_live_or_uncertain_work_refuses_without_changing_database(data, table, schema, row):
    dbfile = data / "snap/jobs.sqlite"
    with sqlite3.connect(dbfile) as db:
        db.execute(f"CREATE TABLE {table}({schema})")
        db.execute(f"INSERT INTO {table} VALUES({row})")
    before = dbfile.read_bytes()
    with pytest.raises(ValueError, match="PENDING_OR_BUSY"):
        host.idle(data)
    assert dbfile.read_bytes() == before


def test_terminal_work_passes_and_media_optional_without_dropping_state(data):
    with sqlite3.connect(data / "snap/jobs.sqlite") as db:
        db.execute("CREATE TABLE jobs(state TEXT)")
        db.execute("INSERT INTO jobs VALUES('COMPLETED')")
    host.idle(data)
    state = host.manifest(data, False)
    assert "snap/jobs.sqlite" in state and "snap/offset.json" in state
    assert "snap/video.mp4" not in state
    assert "snap/video.mp4" in host.manifest(data, True)


def test_symbolic_link_refused(data):
    (data / "snap/escape").symlink_to(data.parent)
    with pytest.raises(ValueError, match="SPECIAL_FILE"):
        host.manifest(data, True)


def test_stage_hash_failure_retains_existing_state(data, monkeypatch):
    monkeypatch.setattr(host, "workers", lambda: [])
    monkeypatch.setattr(host, "stopped", lambda *a: None)
    identifier = "c" * 32
    payload = {"id": identifier}
    host.action("stage", payload, data, OPS.parent.parent)
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        host.action("promote", {**payload, "manifest": {"false": []}}, data, OPS.parent.parent)
    assert (data / "snap/offset.json").is_file()
    assert host.stage_path(data, identifier).is_dir()


def test_promote_preserves_previous_state_without_deletion(data, monkeypatch):
    monkeypatch.setattr(host, "workers", lambda: [])
    monkeypatch.setattr(host, "stopped", lambda *a: None)
    identifier = "d" * 32
    stage = host.stage_path(data, identifier)
    (stage / "snap").mkdir(parents=True)
    (stage / "status").mkdir()
    (stage / "snap/offset.json").write_text('{"offset":99}')
    value = host.manifest(stage, True)
    host.action("promote", {"id": identifier, "manifest": value}, data, OPS.parent.parent)
    assert json.loads((data / "snap/offset.json").read_text())["offset"] == 99
    assert (
        json.loads((data.with_name("state.before-" + identifier) / "snap/offset.json").read_text())[
            "offset"
        ]
        == 12
    )


def test_happy_sequence_and_checkpoint_order(tmp_path):
    fake = Fake()
    task = control.Switch(fake, tmp_path)
    task.forward()
    operations = [(s, o) for s, o, _ in fake.events]
    assert operations.index(("source", "stop")) < operations.index(("source", "copy"))
    assert operations.index(("destination", "promote")) < operations.index(("destination", "start"))
    assert operations[-1] == ("destination", "preview")
    records = [json.loads(p.read_text()) for p in sorted(tmp_path.glob("*.json"))]
    assert records[-1]["phase"] == "completed_previews_without_sending"
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in tmp_path.glob("*.json"))


@pytest.mark.parametrize(
    "failure",
    [
        ("source", "stop"),
        ("source", "copy"),
        ("destination", "promote"),
        ("destination", "start"),
        ("destination", "preview"),
    ],
)
def test_first_failure_stops_no_automatic_replay(tmp_path, failure):
    fake = Fake(failure)
    with pytest.raises(ValueError):
        control.Switch(fake, tmp_path).forward()
    assert (fake.events[-1][0], fake.events[-1][1]) == failure
    assert not any(side == "source" and op == "start" for side, op, _ in fake.events)


def test_rollback_unreachable_destination_never_starts_source(tmp_path):
    fake = Fake(("destination", "stop"))
    previous = {
        "id": "a" * 32,
        "commit": "a" * 40,
        "key_sizes": [10] * 8,
        "units": ["snap", "status"],
        "source_active": ["snap", "status"],
        "phase": "destination_start_intent",
        "source_identity": "source",
        "destination_identity": "destination",
    }
    with pytest.raises(ValueError):
        control.Switch(fake, tmp_path).rollback(previous)
    assert [(s, op) for s, op, _ in fake.events if op != "inspect"] == [("destination", "stop")]


def test_rollback_uses_latest_destination_offsets_before_source_start(tmp_path):
    fake = Fake()
    previous = {
        "id": "b" * 32,
        "commit": "a" * 40,
        "key_sizes": [10] * 8,
        "units": ["snap", "status"],
        "source_active": ["snap", "status"],
        "phase": "destination_start_intent",
        "source_identity": "source",
        "destination_identity": "destination",
    }
    control.Switch(fake, tmp_path).rollback(previous)
    events = [(s, o) for s, o, _ in fake.events]
    assert events.index(("destination", "stop")) < events.index(("destination", "copy"))
    assert events.index(("destination", "copy")) < events.index(("source", "promote"))
    assert events[-1] == ("source", "start")


def test_bad_target_cannot_inject_shell_or_options(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(b"placeholder, not a credential")
    for value in ["-oProxyCommand=evil", "host;echo", "host$(echo)", "host\nother"]:
        with pytest.raises(ValueError, match="TARGET_INVALID"):
            control.Transport(value, key)


def test_open_state_writer_refused_without_reading_process_argv(data, tmp_path):
    proc = tmp_path / "proc"
    fd = proc / "99999999/fd"
    fd.mkdir(parents=True)
    (fd / "1").symlink_to(data / "snap/offset.json")
    with pytest.raises(ValueError, match="STATE_HANDLE_OPEN"):
        host.no_open_state(data, proc)


def test_freeze_then_final_idle_check_prevents_killing_a_new_render(data, monkeypatch):
    names = host.units(OPS.parent.parent)[0]
    events = []
    checks = 0

    def idle(root):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ValueError("new render accepted before freeze")

    monkeypatch.setattr(host, "idle", idle)
    monkeypatch.setattr(host, "workers", lambda: [])
    monkeypatch.setattr(host, "unit_states", lambda ns: {n: "active" for n in ns})
    monkeypatch.setattr(host, "run", lambda args, **kw: events.append(args))
    with pytest.raises(ValueError, match="new render"):
        host.action("stop", {}, data, OPS.parent.parent)
    assert events
    assert not any(e[1] == "stop" for e in events)
    assert [e[2] for e in events if e[1] == "freeze"] == [*names, *host.LEGACY]
    assert [e[2] for e in events if e[1] == "thaw"] == [*names, *host.LEGACY]


def test_credential_files_are_stat_only_in_preflight(tmp_path, monkeypatch):
    keys = []
    for n in range(8):
        key = tmp_path / f"key-{n}"
        key.write_bytes(b"synthetic")
        keys.append(key)
    release = tmp_path / "release"
    release.mkdir()
    (release / "madar-release.json").write_text(
        json.dumps({"commit": "a" * 40, "environment": "PASS"})
    )
    monkeypatch.setattr(host, "units", lambda _: (["one"], keys, ["python3"]))
    monkeypatch.setattr(host.shutil, "which", lambda _: "/fake/tool")
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert path not in keys, "credential content must never be opened"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    assert host.preflight(release)["key_sizes"] == [9] * 8


def test_unsafe_manifest_never_reaches_copy(tmp_path):
    fake = Fake()
    fake.data = {"../../escape": []}
    task = control.Switch(fake, tmp_path)
    with pytest.raises(ValueError, match="MANIFEST_UNSAFE"):
        task.transfer("source", "destination")
    assert all(event[1] != "copy" for event in fake.events)


@pytest.mark.parametrize(
    "field, value",
    [
        ("identity", "source"),
        ("commit", "b" * 40),
        ("key_sizes", [11] * 8),
        ("states", {"snap": "active"}),
    ],
)
def test_destination_mismatch_rejected_before_source_stop(tmp_path, field, value):
    fake = Fake()
    original = fake.call

    def call(side, operation, payload):
        response = original(side, operation, payload)
        if side == "destination" and operation == "inspect":
            response[field] = value
        return response

    fake.call = call
    with pytest.raises(ValueError):
        control.Switch(fake, tmp_path).forward()
    assert not any(op == "stop" for _, op, _ in fake.events)


def test_manifest_changed_during_copy_cannot_start_or_promote(tmp_path):
    fake = Fake()
    original = fake.copy

    def copy(*args):
        original(*args)
        fake.data["snap/offset.json"] = [384, 0, 0, 3, "changed"]

    fake.copy = copy
    with pytest.raises(ValueError, match="SOURCE_CHANGED"):
        control.Switch(fake, tmp_path).forward()
    assert not any(op in {"start", "promote"} for _, op, _ in fake.events)


def test_transfer_argv_preserves_files_and_requires_known_host(tmp_path, monkeypatch):
    key = tmp_path / "ssh-key"
    key.write_bytes(b"placeholder, never opened")
    calls = []
    monkeypatch.setattr(
        control.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs))
    )
    transport = control.Transport("operator@destination", key)
    transport.copy("source", "a" * 32, ["snap/jobs.sqlite", "status/offset.json"])
    argv, kwargs = calls[0]
    assert "--delete" not in argv and "--inplace" not in argv
    assert "StrictHostKeyChecking=yes" in argv[argv.index("-e") + 1]
    assert kwargs["input"] == b"snap/jobs.sqlite\0status/offset.json\0"
    assert "--from0" in argv and "--files-from=-" in argv


def test_rollback_cannot_target_a_different_destination(tmp_path):
    fake = Fake()
    previous = {
        "id": "b" * 32,
        "commit": "a" * 40,
        "key_sizes": [10] * 8,
        "units": ["snap", "status"],
        "source_active": ["snap", "status"],
        "source_identity": "source",
        "destination_identity": "different-destination",
        "phase": "destination_start_intent",
    }
    with pytest.raises(ValueError, match="ROLLBACK_HOST"):
        control.Switch(fake, tmp_path).rollback(previous)
    assert not any(op in {"stop", "copy", "promote", "start"} for _, op, _ in fake.events)


def test_sqlite_wal_preserved_but_host_local_read_marks_not_transferred(data):
    file = data / "snap/jobs.sqlite"
    with sqlite3.connect(file) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE jobs(state TEXT)")
        db.execute("INSERT INTO jobs VALUES('COMPLETED')")
        db.commit()
        host.idle(data)
        manifest = host.manifest(data, False)
        assert "snap/jobs.sqlite" in manifest and "snap/jobs.sqlite-wal" in manifest
        assert "snap/jobs.sqlite-shm" not in manifest
