"""The 18-agent gate (owner decision 18 Nov 2026): no clip leaves unless every agent ran,
succeeded, and started in the approved order. Reads the job's own event log."""

import json
import sqlite3
from pathlib import Path

from .agent_registry import CHAIN, TOOL_IDS

# Sources first; the six oversight roles run inside it; then the clip chain to the gate.
ORDER = (CHAIN[0], *TOOL_IDS, *CHAIN[1:])


def sequence(db: Path) -> tuple[list[str], set[str]]:
    if not db.is_file() or any(p.is_symlink() for p in (db, *db.absolute().parents)):
        raise ValueError("AGENT_CHAIN_LEDGER_MISSING")
    first: dict[str, float] = {}
    passed: set[str] = set()
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT at,details FROM events WHERE event IN ('agent.run','agent.execution')"
            " ORDER BY at,id"
        ).fetchall()
    for at, details in rows:
        value = json.loads(details)
        agent = value.get("agent")
        if not isinstance(agent, str):
            continue
        first.setdefault(agent, float(at))
        if value.get("status") == "SUCCEEDED":
            passed.add(agent)
    return sorted(first, key=lambda a: first[a]), passed


def verify(db: Path) -> tuple[str, ...]:
    started, passed = sequence(db)
    missing = [a for a in ORDER if a not in passed]
    if missing:
        raise ValueError("AGENT_CHAIN_INCOMPLETE: " + " ".join(missing))
    if [a for a in started if a in ORDER] != list(ORDER):
        raise ValueError("AGENT_CHAIN_OUT_OF_ORDER")
    return ORDER
