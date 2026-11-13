"""Configured runtime roots are shared by both bots, the panel and the installer."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from studio_core.config import ConfigError, runtime_state_root

REPO = Path(__file__).resolve().parents[1]


def test_configured_state_is_used_before_any_runtime_is_created(tmp_path):
    root = tmp_path / "isolated-state"
    policy = tmp_path / "policy.toml"
    policy.write_text('[runtime]\nstate_root = ' + json.dumps(str(root)) + "\n")
    code = """
import runpy
from snap_bot import server_preview as snap
from status_bot import server_preview as status
from scripts import control_panel as panel
from studio_core.config import runtime_state_root
root = runtime_state_root()
assert snap.Runtime().root == root / "snap"
assert status.Runtime().root == root / "status"
assert snap.RENDER_WORK == root / "render-work"
assert panel.STATE == root
installer = runpy.run_path("apps/snap-bot/snap_bot/release.py")
assert installer["STATE"] == root
assert installer["DATA"] == root / "snap"
assert installer["STATUS_DATA"] == root / "status"
assert "ReadWritePaths=" + str(root) + "\\n" in installer["unit_text"]()
assert "ReadWritePaths=" + str(root / "status") + "\\n" in installer["status_unit_text"]()
assert not root.exists()
"""
    env = {
        **os.environ,
        "STUDIO_CONFIG": str(policy),
        "PYTHONPATH": os.pathsep.join(
            str(REPO / path)
            for path in (".", "packages/studio-core", "apps/snap-bot", "apps/status-bot")
        ),
    }
    result = subprocess.run(  # noqa: S603 - fixed Python interpreter and isolated config
        [sys.executable, "-c", code],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not root.exists()


@pytest.mark.parametrize("value", ["", "relative", "/tmp/../state", 17, True, []])
def test_invalid_state_config_is_rejected_before_io(tmp_path, value):
    policy = tmp_path / "policy.toml"
    if isinstance(value, list):
        literal = "[]"
    else:
        literal = json.dumps(value)
    policy.write_text("[runtime]\nstate_root = " + literal + "\n")
    with pytest.raises(ConfigError):
        runtime_state_root(policy)


def test_default_state_root_is_unchanged():
    assert runtime_state_root(REPO / "config/defaults.toml") == Path("/var/lib/madar")


def test_older_policy_without_state_root_keeps_default(tmp_path):
    policy = tmp_path / "policy.toml"
    policy.write_text("[runtime]\nrender_concurrency = 1\n")
    assert runtime_state_root(policy) == Path("/var/lib/madar")
