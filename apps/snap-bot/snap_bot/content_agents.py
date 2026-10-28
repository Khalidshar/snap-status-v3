"""Four deterministic content agents for curated did_you_know facts.

Each agent checks the reserved content and passes it through unchanged. No AI,
network, rewriting or new selection: memory and rotation independently verify
the decision choose_fact already reserved, from the owned database state.
"""

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from studio_core.jobs import Repository

from .arabic_agent import ABBREVIATIONS, check_sacred
from .did_you_know import (
    CATALOG,
    CATEGORIES,
    DEFAULT_KIND,
    categories_of,
    content_kind,
    is_sacred,
    load_facts,
    load_kind,
    reservation_state,
    rotation_order,
    validate_content,
)

RULE_VERSION = "content-agents-v1"
CHECKS = ("memory", "rotation", "proofreading", "translation")
ARABIC_PUNCTUATION = "،؛؟.:!"


class AgentRejected(ValueError):
    """A check agent refused the content; the job must not reach rendering."""


def _reservation(db: sqlite3.Connection, job: str) -> Any:
    row = db.execute(
        "SELECT fact_id,body_hash,reserved_at,content_json FROM fact_reservations WHERE job_id=?",
        (job,),
    ).fetchone()
    if not row:
        raise AgentRejected("RESERVATION_MISSING")
    return row


def check_memory(db: sqlite3.Connection, job: str, content: dict[str, Any]) -> dict[str, Any]:
    """مراجع التكرار: the fact and its exact wording are outside every protected window."""
    row = _reservation(db, job)
    if json.loads(row["content_json"]) != content:
        raise AgentRejected("MEMORY_CONTENT_NOT_RESERVED")
    body_hash = hashlib.sha256(content["body"].encode()).hexdigest()
    if row["fact_id"] != content["fact_id"] or row["body_hash"] != body_hash:
        raise AgentRejected("MEMORY_RESERVATION_MISMATCH")
    blocked, _ = reservation_state(
        db, row["reserved_at"], exclude_job=job, kind=content_kind(content)
    )
    if ("id", content["fact_id"]) in blocked or ("body", body_hash) in blocked:
        raise AgentRejected("MEMORY_REPEAT")
    return {"decision": "PASS", "protected_items": len(blocked) // 2}


def check_rotation(
    db: sqlite3.Connection,
    job: str,
    content: dict[str, Any],
    *,
    catalog: str | Path | None = None,
) -> dict[str, Any]:
    """التناوب: the category is the first in rotation order that still had an eligible fact."""
    row = _reservation(db, job)
    kind = content_kind(content)
    blocked, last = reservation_state(db, row["reserved_at"], exclude_job=job, kind=kind)
    day = datetime.fromtimestamp(row["reserved_at"], UTC).date()
    categories: tuple[str, ...]
    if kind == DEFAULT_KIND:
        facts, categories = load_facts(catalog or CATALOG, day), CATEGORIES
    else:
        facts = load_kind(kind, day)
        categories = categories_of(facts)
    for category in rotation_order(last, categories):
        eligible = [
            fact
            for fact in facts
            if fact["category"] == category
            and ("id", fact["id"]) not in blocked
            and ("body", hashlib.sha256(fact["text"].encode()).hexdigest()) not in blocked
        ]
        if eligible:
            if content["category"] != category:
                raise AgentRejected("ROTATION_SKIPPED_CATEGORY")
            return {"decision": "PASS", "previous": last, "category": category}
    raise AgentRejected("ROTATION_NO_ELIGIBLE_CATEGORY")


def proofread(text: str) -> dict[str, Any]:
    """التدقيق: spacing and punctuation hygiene; reports, never edits."""
    problems: list[str] = []
    if "  " in text:
        problems.append("DOUBLE_SPACE")
    if re.search(r"\s[" + re.escape(ARABIC_PUNCTUATION) + "]", text):
        problems.append("SPACE_BEFORE_PUNCTUATION")
    if re.search(r"([" + re.escape(ARABIC_PUNCTUATION) + r"])\1", text):
        problems.append("REPEATED_PUNCTUATION")
    if "ـ" in text:
        problems.append("TATWEEL")
    if text.count("«") != text.count("»") or text.count("(") != text.count(")"):
        problems.append("UNBALANCED_QUOTES")
    if problems:
        raise AgentRejected("PROOFREADING_" + problems[0])
    return {"decision": "PASS", "checks": ["spacing", "punctuation", "tatweel", "balanced_pairs"]}


def translation_requirement(text: str) -> dict[str, Any]:
    """الترجمة: Latin terms must be approved abbreviations; nothing else needs translating."""
    terms = re.findall(r"[A-Za-z][A-Za-z0-9]*(?:\s[A-Z][A-Za-z0-9]*)?", text)
    approved = [
        term for term in ABBREVIATIONS if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text)
    ]
    leftover = re.sub(
        r"(?<!\w)(?:" + "|".join(re.escape(term) for term in ABBREVIATIONS) + r")(?!\w)", "", text
    )
    if re.search(r"[A-Za-z]", leftover):
        raise AgentRejected("TRANSLATION_REQUIRED")
    return {
        "decision": "PASS",
        "required": False,
        "approved_terms": approved,
        "latin_terms_seen": len(terms),
    }


def run_check(
    role: str, repo: Repository, job: str, token: str, attempt: int, content: dict[str, Any]
) -> dict[str, Any]:
    """Run one check agent under the current lease; return the content unchanged."""
    if role not in CHECKS:
        raise ValueError("Unknown content agent")
    validate_content(content)
    if role in ("memory", "rotation"):
        with repo.owned_transaction(job, token, attempt=attempt) as db:
            (check_memory if role == "memory" else check_rotation)(db, job, content)
    elif is_sacred(content):
        # Faith text is not proofread or translated: the arabic agent already proved
        # it is the approved text, byte for byte.
        check_sacred(content.get("kind"), content["fact_id"], content["body"])
    elif role == "proofreading":
        proofread(content["body"])
    else:
        translation_requirement(content["body"])
    return content
