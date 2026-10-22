"""Analyst and manager: read-only views over the owned database and catalog.

The analyst summarises what happened; the manager says whether the next
did_you_know should run and what it would pick, without reserving, claiming,
rendering or sending anything. No AI and no network.
"""

import argparse
import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from studio_core.config import Config
from studio_core.jobs import Repository

from .agent_contract_map import BOUND
from .did_you_know import CATALOG, CATEGORIES, load_facts, reservation_state, rotation_order
from .maintenance import failure_code

RULE_VERSION = "oversight-local-v1"
REPEATED_FAILURES = 3
LOW_STOCK = 2
# A video waiting for the owner's frame review (VALIDATING) does not block the next one.
RUNNING = "state NOT IN ('COMPLETED','FAILED','CANCELLED','EXPIRED','VALIDATING')"


def _table(db, name):
    return (
        db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        is not None
    )


def _stock(db, now, catalog):
    """Facts each category could still use now, outside every protected window."""
    if _table(db, "fact_reservations"):
        blocked, last = reservation_state(db, now)
    else:
        blocked, last = set(), None
    stock, first = Counter(), {}
    for fact in load_facts(catalog):
        if ("id", fact["id"]) not in blocked and (
            "body",
            hashlib.sha256(fact["text"].encode()).hexdigest(),
        ) not in blocked:
            stock[fact["category"]] += 1
            first.setdefault(fact["category"], fact["id"])
    return {category: stock[category] for category in CATEGORIES}, last, first


def analyse(repo, *, now=None, window=7 * 86400, catalog=CATALOG):
    """المحلّل: counts and causes for the owner; changes nothing."""
    now = repo.clock() if now is None else now
    with repo.connect() as db:
        jobs = db.execute(
            "SELECT * FROM jobs WHERE created_at>? ORDER BY created_at,id", (now - window,)
        ).fetchall()
        by_state = Counter((job["command"], job["state"]) for job in jobs)
        failures = Counter(
            failure_code(db, job) or "UNKNOWN" for job in jobs if job["state"] == "FAILED"
        )
        rejections = Counter()
        if _table(db, "owned_agent_contracts"):
            for row in db.execute(
                "SELECT c.role,c.error_code FROM owned_agent_contracts c JOIN jobs j ON j.id=c.job_id WHERE c.status='FAILED' AND j.created_at>?",
                (now - window,),
            ):
                rejections[row["role"] + ":" + (row["error_code"] or "UNKNOWN")] += 1
        repairs = (
            db.execute(
                "SELECT COUNT(*) FROM maintenance_repairs WHERE repaired_at>?", (now - window,)
            ).fetchone()[0]
            if _table(db, "maintenance_repairs")
            else 0
        )
        stock, last, _ = _stock(db, now, catalog)
    facts = [job for job in jobs if job["command"] == "did_you_know"]
    finished = [job for job in facts if job["state"] in ("VALIDATING", "COMPLETED", "FAILED")]
    good = sum(job["state"] != "FAILED" for job in finished)
    report = {
        "schema": 1,
        "rule_version": RULE_VERSION,
        "analysed_at": now,
        "window_seconds": window,
        "jobs": {
            f"{command}:{state}": count for (command, state), count in sorted(by_state.items())
        },
        "did_you_know_success_rate": round(good / len(finished), 3) if finished else None,
        "failure_causes": dict(failures.most_common()),
        "agent_rejections": dict(rejections.most_common()),
        "repairs": repairs,
        "fact_stock": stock,
        "last_category": last,
        "low_stock": sorted(category for category, count in stock.items() if count < LOW_STOCK),
    }
    lines = [f"مهام «هل تعلم» المنتهية: {len(finished)}، الناجحة منها: {good}"]
    if failures:
        lines.append(
            "أكثر أسباب الفشل: "
            + "، ".join(f"{code} ({count})" for code, count in failures.most_common(3))
        )
    if report["low_stock"]:
        lines.append("فئات قارب مخزونها على النفاد: " + "، ".join(report["low_stock"]))
    report["summary_ar"] = lines
    return report


def plan(repo, *, now=None, catalog=CATALOG):
    """المدير: GO or HOLD for the next did_you_know, with the choice it would make."""
    now = repo.clock() if now is None else now
    reasons = []
    with repo.connect() as db:
        active = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE " + RUNNING + " AND expires_at>?", (now,)
        ).fetchone()[0]
        awaiting = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE state='VALIDATING' AND expires_at>?", (now,)
        ).fetchone()[0]
        recent = db.execute(
            "SELECT state FROM jobs WHERE command='did_you_know' AND state IN ('VALIDATING','COMPLETED','FAILED') ORDER BY updated_at DESC,rowid DESC LIMIT ?",
            (REPEATED_FAILURES,),
        ).fetchall()
        stock, last, first = _stock(db, now, catalog)
    if active:
        reasons.append("QUEUE_BUSY")
    if len(recent) == REPEATED_FAILURES and all(row["state"] == "FAILED" for row in recent):
        reasons.append("REPEATED_FAILURES")
    category = next((c for c in rotation_order(last) if stock[c] > 0), None)
    if category is None:
        reasons.append("CATALOG_EXHAUSTED")
    result = {
        "schema": 1,
        "rule_version": RULE_VERSION,
        "planned_at": now,
        "command": "did_you_know",
        "decision": "HOLD" if reasons else "GO",
        "reasons": reasons,
        "next_category": category,
        "next_fact_id": first.get(category),
        "awaiting_owner_review": awaiting,
        "chain": list(BOUND) + ["final"],
        "delivery": "MANUAL_AFTER_HUMAN_FRAME",
        "reserves": False,
        "claims": False,
        "sends": False,
    }
    labels = {
        "QUEUE_BUSY": "توجد مهمة قيد التنفيذ",
        "REPEATED_FAILURES": "فشلت آخر ثلاث مهام؛ راجع التحليل أولًا",
        "CATALOG_EXHAUSTED": "لا توجد حقيقة متاحة خارج نافذة التكرار",
    }
    result["summary_ar"] = [labels[reason] for reason in reasons] or [
        f"جاهز: الفئة التالية {category}"
    ]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only analyst report and manager plan")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("role", choices=["analyst", "manager"])
    args = parser.parse_args(argv)
    repo = Repository(Config.load(args.config))
    output = (
        analyse(repo, now=time.time()) if args.role == "analyst" else plan(repo, now=time.time())
    )
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
