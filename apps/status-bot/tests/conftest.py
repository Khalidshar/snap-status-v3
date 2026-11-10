"""The ported lab tests share fixtures by importing sibling test modules."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _panel_queue_is_never_the_live_one(tmp_path_factory, monkeypatch):
    """No test may open the running bot's panel queue under /var/lib/madar (repair 11 Nov)."""
    from status_bot import owner_commands

    monkeypatch.setattr(owner_commands, "STATE", tmp_path_factory.mktemp("status-state"))
