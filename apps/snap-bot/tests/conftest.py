"""The ported lab tests share fixtures by importing sibling test modules."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _chain_gate_ledger(request, monkeypatch):
    """Send-path fixtures carry no 18-agent ledger; the gate has its own tests
    (test_chain_gate.py) and a hook test per bot. Mark a test real_chain_gate to keep it."""
    if request.node.get_closest_marker("real_chain_gate"):
        return
    from studio_core.observability import chain_gate

    monkeypatch.setattr(chain_gate, "verify", lambda db: chain_gate.ORDER)
