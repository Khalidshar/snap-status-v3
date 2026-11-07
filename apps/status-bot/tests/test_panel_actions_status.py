"""WhatsApp panel executor: saved choices close cleanly; approve refuses a changed clip."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from status_bot import panel_actions
from studio_core.jobs import panel_workspace as workspace


def test_rate_is_saved_and_server_stop_halts(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "panel-commands.sqlite"
    changes: list[bool] = []
    monkeypatch.setattr(panel_actions, "change", lambda stop, runtime, tag: changes.append(stop))
    workspace.submit(
        path, "1" * 32, "server", {"target": "wsl", "operation": "stop", "confirmed": True}
    )
    assert panel_actions.process(None, None, SimpleNamespace(), path)
    assert changes == [True]
    assert workspace.view(path)["actions"][0]["state"] == "DONE"


def test_approve_refuses_a_clip_that_changed_after_review(tmp_path: Path) -> None:
    path = tmp_path / "panel-commands.sqlite"
    (tmp_path / "panel-media").mkdir()
    (tmp_path / "panel-media" / ("a" * 32 + ".mp4")).write_bytes(b"other bytes")
    with pytest.raises(ValueError):
        panel_actions.approve(None, None, None, path, "a" * 32, "0" * 64)
