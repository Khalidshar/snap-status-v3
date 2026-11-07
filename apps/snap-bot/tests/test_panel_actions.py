"""Snap panel executor: produce → preview, approve sends that SHA once, reject cancels."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from snap_bot import owner_commands, panel_actions
from studio_core.jobs import panel_workspace as workspace

PREVIEW = "a" * 32


class FakeStudio:
    def __init__(self, root: Path) -> None:
        self.runtime = SimpleNamespace(root=root)
        self.started: list[str] = []
        self.delivered: list[str] = []
        self.saved: dict = {"confirm": None, "cancelled": []}
        self.halted = False

    def start(self, unit: str) -> int:
        self.started.append(unit)
        folder = self.runtime.root / "previews" / PREVIEW
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "did-you-know.mp4").write_bytes(b"clip-bytes")
        return 0

    def pending(self) -> dict:
        sha = hashlib.sha256(b"clip-bytes").hexdigest()
        return {"sha": sha, "preview": PREVIEW, "kind": "isha", "title": "ليلة طيبة"}

    def deliver(self, sha: str) -> str:
        self.delivered.append(sha)
        return owner_commands.SENT + sha[:8]

    def state(self) -> dict:
        return self.saved

    def save(self, state: dict) -> None:
        self.saved = state

    def halt(self) -> str:
        self.halted = True
        return ""

    def unhalt(self) -> str:
        self.halted = False
        return ""


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(owner_commands, "HALT", tmp_path / "HALT")
    path = tmp_path / "panel-commands.sqlite"
    return FakeStudio(tmp_path), path


def submit(path: Path, action: str, payload: dict, n: int) -> None:
    workspace.submit(path, f"{n:032x}", action, payload)


def test_produce_records_a_preview_and_sends_nothing(setup) -> None:
    studio, path = setup
    submit(path, "produce", {"command": "isha"}, 1)
    assert panel_actions.process(studio, path)
    assert studio.started == ["madar-v3-snap-render@isha.service"]
    assert studio.delivered == []
    [preview] = workspace.view(path)["previews"]
    assert preview["id"] == PREVIEW and preview["state"] == "REVIEW"


def test_approve_sends_that_exact_sha_once_and_reject_cancels(setup) -> None:
    studio, path = setup
    submit(path, "produce", {"command": "isha"}, 1)
    panel_actions.process(studio, path)
    sha = workspace.view(path)["previews"][0]["sha"]
    submit(path, "approve", {"preview": PREVIEW, "sha": sha}, 2)
    assert panel_actions.process(studio, path)
    assert studio.delivered == [sha]
    assert not panel_actions.process(studio, path)  # queue empty: nothing resent


def test_halt_blocks_production(setup, tmp_path: Path) -> None:
    studio, path = setup
    (tmp_path / "HALT").touch()
    submit(path, "produce", {"command": "verse"}, 3)
    panel_actions.process(studio, path)
    assert studio.started == []
    assert any(a["state"] == "UNKNOWN" for a in workspace.view(path)["actions"])


def test_server_stop_halts_only_this_server(setup) -> None:
    studio, path = setup
    submit(path, "server", {"target": "wsl", "operation": "stop", "confirmed": True}, 4)
    panel_actions.process(studio, path)
    assert studio.halted
