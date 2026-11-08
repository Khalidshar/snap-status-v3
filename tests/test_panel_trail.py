"""Panel shows each clip's agents live (owner decision 7 Nov 2026)."""

import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def panel():
    spec = importlib.util.spec_from_file_location(
        "control_panel", ROOT / "scripts/control_panel.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clip(
    root: Path, sub: str, name: str, runs: list[tuple[str, str, float, int, str | None]]
) -> None:
    db = root / sub / name / "state/jobs.sqlite"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as c:
        c.execute(
            "CREATE TABLE agent_runs(role TEXT,status TEXT,started_at REAL,duration_ms INT,"
            "error_code TEXT)"
        )
        c.executemany("INSERT INTO agent_runs VALUES(?,?,?,?,?)", runs)


def test_trail_lists_each_agent_and_failure_code_without_text(tmp_path: Path) -> None:
    module = panel()
    clip(tmp_path, "status/content-clips", "a" * 64, [
        ("fact_source", "SUCCEEDED", 10.0, 5, None),
        ("manager", "SUCCEEDED", 11.0, 3, None),
        ("design", "FAILED", 12.0, 9, "TIMEOUT"),
    ])  # fmt: skip
    clip(tmp_path, "snap/previews", "b" * 32, [("writer", "RUNNING", 13.0, None, None)])
    trail = module.clip_trail(tmp_path)
    status = next(c for c in trail if c["app"] == "status")
    assert status["state"] == "FAILED"
    assert [s["role"] for s in status["steps"]] == ["fact_source", "manager", "design"]
    assert status["steps"][-1]["code"] == "TIMEOUT"
    assert next(c for c in trail if c["app"] == "snap")["state"] == "RUNNING"


def test_clip_records_fill_agents_the_ledger_never_saw() -> None:
    module = panel()
    state = {"apps": {"status": {"agents": [{"id": "manager", "state": "UNKNOWN", "at": None}]}}}
    trail = [{"app": "status", "at": 50.0, "steps": [
        {"role": "manager", "state": "SUCCEEDED", "at": 40.0, "ms": 3, "code": None},
    ]}]  # fmt: skip
    module.with_trail(state, trail)
    assert state["apps"]["status"]["agents"][0] == {
        "id": "manager",
        "state": "SUCCEEDED",
        "at": 40.0,
    }


def test_panel_on_wsl_reads_this_machine_directly(monkeypatch, tmp_path) -> None:
    module = panel()
    monkeypatch.setattr(module, "STATE", tmp_path)
    monkeypatch.setattr(module, "ctl", lambda action, unit: unit.endswith("commands.service"))
    value = module.local_metrics()
    assert value["memory_total"] > 0 and value["disk_total"] > 0
    assert value["services"] == {
        "snap": "active", "status": "active", "snap_old": "inactive", "status_old": "inactive"
    }  # fmt: skip
