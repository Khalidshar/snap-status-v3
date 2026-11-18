import json
import sqlite3

import pytest

from studio_core.observability import chain_gate

APPROVED = (
    "fact_source", "manager", "analyst", "writer", "reviewer", "fixer", "content_repair",
    "arabic", "memory", "rotation", "proofreading", "translation", "design", "voice",
    "media_verification", "judge", "quality", "final",
)  # fmt: skip


def ledger(tmp_path, order, failed=()):
    db = tmp_path / "jobs.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE events(id INTEGER PRIMARY KEY, at REAL, event TEXT, details TEXT)"
        )
        for n, agent in enumerate(order):
            status = "FAILED" if agent in failed else "SUCCEEDED"
            for k, state in enumerate(("RUNNING", status)):
                conn.execute(
                    "INSERT INTO events(at,event,details) VALUES(?,?,?)",
                    (n + k / 10, "agent.run", json.dumps({"agent": agent, "status": state})),
                )
    return db


def test_the_registry_order_is_the_owners_approved_order():
    assert chain_gate.ORDER == APPROVED and len(chain_gate.ORDER) == 18


def test_all_eighteen_in_order_pass(tmp_path):
    assert chain_gate.verify(ledger(tmp_path, APPROVED)) == APPROVED


def test_a_skipped_agent_blocks_the_send(tmp_path):
    with pytest.raises(ValueError, match="AGENT_CHAIN_INCOMPLETE: judge"):
        chain_gate.verify(ledger(tmp_path, [a for a in APPROVED if a != "judge"]))


def test_a_failed_agent_blocks_the_send(tmp_path):
    with pytest.raises(ValueError, match="AGENT_CHAIN_INCOMPLETE: quality"):
        chain_gate.verify(ledger(tmp_path, APPROVED, failed={"quality"}))


def test_out_of_order_blocks_the_send(tmp_path):
    swapped = list(APPROVED)
    swapped[12], swapped[13] = swapped[13], swapped[12]
    with pytest.raises(ValueError, match="AGENT_CHAIN_OUT_OF_ORDER"):
        chain_gate.verify(ledger(tmp_path, swapped))


def test_missing_ledger_blocks_the_send(tmp_path):
    with pytest.raises(ValueError, match="AGENT_CHAIN_LEDGER_MISSING"):
        chain_gate.verify(tmp_path / "none.sqlite")


def test_news_words_are_not_calls_to_action():
    from studio_core.production.audit import FORBIDDEN_IN_CONTENT

    assert not FORBIDDEN_IN_CONTENT.search("تابعت الوزارة الحالة وشاركت الدول في القمة")
    assert FORBIDDEN_IN_CONTENT.search("تابعني لكل جديد")
    assert FORBIDDEN_IN_CONTENT.search("شاركها مع أصدقائك")
