"""Writer, reviewer and fixer for owner-proposed did_you_know facts.

Local and deterministic: no AI, network or catalog write. A proposal ends as
READY_FOR_OWNER or REJECTED; only the owner adds a fact to the catalog, in a
reviewed commit. The fixer edits spacing and punctuation only, never words.
"""

import argparse
import json
import re
import textwrap
import unicodedata
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .arabic_agent import check_text
from .content_agents import AgentRejected, proofread, translation_requirement
from .did_you_know import CATALOG, CATEGORIES, validate_fact

RULE_VERSION = "editorial-local-v1"
LINE_WIDTH = 20  # The pinned fact profile wraps at 20 characters.
MAX_LINES = 5
# Shared words over the shorter fact; the reviewed catalog peaks at 0.25.
SIMILARITY_LIMIT = 0.6
FIXABLE = (
    "PROOFREADING_DOUBLE_SPACE",
    "PROOFREADING_SPACE_BEFORE_PUNCTUATION",
    "PROOFREADING_REPEATED_PUNCTUATION",
    "PROOFREADING_TATWEEL",
    "ARABIC_PUNCTUATION_INVALID",
)
PROPOSAL_KEYS = {"category", "text", "source", "source_url", "verified_at"}
MARKS = re.compile("[ً-ٰٕ]")


def catalog_facts(path: str | Path = CATALOG) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))["facts"]


def skeleton(text: str) -> str:
    """Letters and digits only: what a meaning-preserving fix may never change."""
    plain = MARKS.sub("", unicodedata.normalize("NFC", text)).replace("ـ", "")
    return "".join(c for c in plain if unicodedata.category(c)[0] in "LN")


def words(text: str) -> set[str]:
    return set(re.findall(r"[ء-غف-ي0-9A-Za-z]+", MARKS.sub("", text)))


def write(proposal: Any, facts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """الكاتب: turn an approved source into a catalog-shaped draft; never invents text."""
    if not isinstance(proposal, dict) or set(proposal) != PROPOSAL_KEYS:
        raise AgentRejected("WRITER_PROPOSAL_INCOMPLETE")
    if proposal["category"] not in CATEGORIES:
        raise AgentRejected("WRITER_CATEGORY_UNKNOWN")
    used = {int(f["id"][4:]) for f in facts}
    free = next((n for n in range(1, 100) if n not in used), None)
    if free is None:
        raise AgentRejected("WRITER_CATALOG_FULL")
    text = (
        unicodedata.normalize("NFC", proposal["text"])
        if isinstance(proposal["text"], str)
        else proposal["text"]
    )
    return {
        "id": f"dyk-{free:02d}",
        "category": proposal["category"],
        "text": text,
        "source": proposal["source"],
        "source_url": proposal["source_url"],
        "verified_at": proposal["verified_at"],
    }


def review(
    draft: dict[str, Any], facts: Sequence[dict[str, Any]], *, today: date | None = None
) -> dict[str, Any]:
    """المراجع: every local rule at once, so the fixer and the owner see all problems."""
    today = today or datetime.now(UTC).date()
    problems: list[str] = []
    try:
        validate_fact(draft, today)
    except ValueError as error:
        problems.append("SOURCE_" + re.sub("[^A-Z]+", "_", str(error).upper()).strip("_"))
    text = draft.get("text")
    if isinstance(text, str):
        for check in (check_text, proofread, translation_requirement):
            try:
                # Tatweel is reported once, as the fixable proofreading finding below.
                check(text.replace("ـ", "") if check is check_text else text)
            except ValueError as error:
                code = str(error)
                if code not in problems:
                    problems.append(code)
        # proofread stops at its first finding; list every fixable one.
        for code, found in (
            ("PROOFREADING_DOUBLE_SPACE", "  " in text),
            ("PROOFREADING_SPACE_BEFORE_PUNCTUATION", re.search(r"\s[،؛؟.:!]", text)),
            ("PROOFREADING_REPEATED_PUNCTUATION", re.search(r"([،؛؟.:!])\1", text)),
            ("PROOFREADING_TATWEEL", "ـ" in text),
        ):
            if found and code not in problems:
                problems.append(code)
        lines = textwrap.wrap(
            text, width=LINE_WIDTH, break_long_words=False, break_on_hyphens=False
        )
        if not 1 <= len(lines) <= MAX_LINES or any(len(line) > LINE_WIDTH for line in lines):
            problems.append("LAYOUT_TOO_LONG")
        mine = words(text)
        for fact in facts:
            theirs = words(fact["text"])
            if skeleton(fact["text"]) == skeleton(text):
                problems.append("DUPLICATE_OF_" + fact["id"])
            elif (
                mine
                and theirs
                and len(mine & theirs) / min(len(mine), len(theirs)) >= SIMILARITY_LIMIT
            ):
                problems.append("SIMILAR_TO_" + fact["id"])
    return {
        "version": RULE_VERSION,
        "decision": "REJECT" if problems else "APPROVE",
        "problems": problems,
    }


def fix(draft: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any] | None:
    """المصحّح: spacing and punctuation only; refuses when a problem needs words changed."""
    if decision["decision"] != "REJECT" or any(
        code not in FIXABLE for code in decision["problems"]
    ):
        return None
    text = draft["text"].replace("ـ", "")
    text = re.sub(
        r"(?<![\d,])(\d{1,3}(?:,\d{3})+)(?![\d,])", lambda m: m[1].replace(",", "\x00"), text
    )
    text = text.translate({ord(","): "،", ord(";"): "؛", ord("?"): "؟"}).replace("\x00", ",")
    text = re.sub(r"\s+([،؛؟.:!])", r"\1", text)
    text = re.sub(r"([،؛؟.:!])\1+", r"\1", text)
    text = re.sub(r" {2,}", " ", text).strip()
    if skeleton(text) != skeleton(draft["text"]):
        raise AgentRejected("FIXER_CHANGED_WORDS")
    return {**draft, "text": text}


def propose(
    proposal: Any, *, catalog: str | Path = CATALOG, today: date | None = None
) -> dict[str, Any]:
    """Writer, reviewer, at most one fixer round, then the reviewer again."""
    facts = catalog_facts(catalog)
    try:
        draft = write(proposal, facts)
    except AgentRejected as error:
        return {
            "status": "REJECTED",
            "stage": "writer",
            "problems": [str(error)],
            "catalog_written": False,
        }
    decision = review(draft, facts, today=today)
    fixed = None
    if decision["decision"] == "REJECT":
        fixed = fix(draft, decision)
        if fixed is not None:
            draft, decision = fixed, review(fixed, facts, today=today)
    return {
        "status": "READY_FOR_OWNER" if decision["decision"] == "APPROVE" else "REJECTED",
        "stage": "reviewer",
        "draft": draft,
        "review": decision,
        "fixed": fixed is not None,
        "problems": decision["problems"],
        "catalog_written": False,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Check a proposed fact; never writes the catalog")
    parser.add_argument("--proposal", type=Path, required=True)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            propose(json.loads(args.proposal.read_text(encoding="utf-8"))),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
