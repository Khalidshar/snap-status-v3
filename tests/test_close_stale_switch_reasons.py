"""Refusal diagnostics use a fixed vocabulary, never private exception text."""

import importlib.util
import sqlite3
import subprocess
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


load("switch_host")
utility = load("close_stale_switch_rows")


@pytest.mark.parametrize(
    "error, expected",
    [
        (utility.ReviewRefused("CUTOFF_TOO_RECENT"), "CUTOFF_TOO_RECENT"),
        (ValueError("SWITCH_CONSUMER_STILL_RUNNING"), "SWITCH_CONSUMER_STILL_RUNNING"),
        (ValueError("SWITCH_UNSUPERVISED_CONSUMER"), "SWITCH_UNSUPERVISED_CONSUMER"),
        (ValueError("SWITCH_STATE_HANDLE_OPEN"), "SWITCH_STATE_HANDLE_OPEN"),
        (ValueError("STALE_REVIEW_COMPONENT_ACTIVE"), "STALE_REVIEW_COMPONENT_ACTIVE"),
        (BlockingIOError("private lock path"), "LOCK_BUSY"),
        (PermissionError("private path"), "PERMISSION_DENIED"),
        (FileNotFoundError("private path"), "FILE_MISSING"),
        (sqlite3.OperationalError("private SQL"), "DATABASE_ERROR"),
        (
            subprocess.CalledProcessError(1, ["private argv"], stderr=b"private stderr"),
            "SYSTEM_CHECK_FAILED",
        ),
        (ValueError("private value"), "VALIDATION_FAILED"),
        (OSError("private path"), "OS_ERROR"),
    ],
)
def test_cli_reports_specific_reason_without_exception_details(
    monkeypatch, capsys, error, expected
):
    monkeypatch.setattr(
        sys, "argv", ["close_stale_switch_rows", "--before-unix", "1", "--reason", "OWNER_REVIEWED"]
    )

    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(utility, "review", refuse)
    with pytest.raises(SystemExit) as result:
        utility.main()
    assert result.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert f"STALE_REVIEW_REFUSED reason={expected};" in output.err
    assert "private" not in output.err


@pytest.mark.parametrize("age, accepted", [(3599.999, False), (3600, True), (3600.001, True)])
def test_one_hour_boundary_is_unchanged(tmp_path, monkeypatch, age, accepted):
    monkeypatch.setattr(utility.time, "time", lambda: 10000)
    if accepted:
        assert utility.review(tmp_path, "both", 10000 - age, "OWNER_REVIEWED") == []
    else:
        monkeypatch.setattr(utility.host, "path_safe", lambda _: pytest.fail("must refuse first"))
        with pytest.raises(utility.ReviewRefused) as error:
            utility.review(tmp_path, "both", 10000 - age, "OWNER_REVIEWED", apply=True)
        assert utility.refusal_reason(error.value) == "CUTOFF_TOO_RECENT"


def test_database_busy_has_fixed_reason():
    error = sqlite3.OperationalError("private database path")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    assert utility.refusal_reason(error) == "DATABASE_LOCKED"


def test_live_render_lock_still_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(utility.host, "units", lambda: ([], [], []))
    monkeypatch.setattr(utility.host, "workers", lambda: [])
    monkeypatch.setattr(utility.host, "stopped", lambda *args: None)
    path = tmp_path / "snap/lock"
    path.parent.mkdir()
    path.write_bytes(b"")

    def busy(*args):
        raise BlockingIOError("private lock path")

    monkeypatch.setattr(utility.fcntl, "flock", busy)
    with pytest.raises(BlockingIOError) as error:
        utility.require_quiet(tmp_path)
    assert utility.refusal_reason(error.value) == "LOCK_BUSY"
