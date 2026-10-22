"""Declared contracts and a READ-ONLY view of owned Snap run events.

This is not an execution engine, recovery controller or delivery authority.
SUCCEEDED here means an observer event, never acceptance of all role duties.
"""

import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing
from pathlib import Path

ROLE_IDS = (
    "manager",
    "memory",
    "writer",
    "reviewer",
    "fixer",
    "quality",
    "judge",
    "analyst",
    "translation",
    "arabic",
    "proofreading",
    "design",
    "rotation",
    "fact_source",
    "media_verification",
    "voice",
    "final",
    "content_repair",
)
BOUND = (
    "fact_source",
    "arabic",
    "memory",
    "rotation",
    "proofreading",
    "translation",
    "design",
    "voice",
    "media_verification",
    "judge",
    "quality",
)
# Check agents verify their input and pass it through byte-identical: content
# checks before the render, render checks between the render and adoption.
CONTENT_CHECKS = ("arabic", "memory", "rotation", "proofreading", "translation")
RENDER_CHECKS = ("voice", "media_verification", "judge")
PASS_THROUGH = CONTENT_CHECKS + RENDER_CHECKS
OBSERVED = BOUND + ("final",)
# Roles outside the per-job chain: local tools the owner or maintenance runs.
LOCAL_TOOLS = {
    "manager": "oversight_agents.plan",
    "analyst": "oversight_agents.analyse",
    "writer": "editorial_agents.write",
    "reviewer": "editorial_agents.review",
    "fixer": "editorial_agents.fix",
    "content_repair": "maintenance.apply",
}
DEPENDENCIES = {
    "fact_source": (),
    "arabic": ("fact_source",),
    "memory": ("arabic",),
    "rotation": ("memory",),
    "proofreading": ("rotation",),
    "translation": ("proofreading",),
    "design": ("translation",),
    "voice": ("design",),
    "media_verification": ("voice",),
    "judge": ("media_verification",),
    "quality": ("judge",),
    "final": ("quality",),
}
# Unbound interfaces remain future work. Check agents have local deterministic
# rules (content_agents.py, render_agents.py); none uses AI or the network.
INTERFACES = {
    "manager": ("owner_command", "content_plan"),
    "memory": ("candidate_ids,memory_window", "eligible_ids"),
    "writer": ("approved_source", "draft"),
    "reviewer": ("draft,source", "review_decision"),
    "fixer": ("draft,rejection", "corrected_draft"),
    "quality": ("attempt_render_result", "owned_stage_receipt"),
    "judge": ("attempt_render_result,owned_agent_ledger", "unchanged_judged_render_result"),
    "analyst": ("owner_statistics", "analysis"),
    "translation": ("draft,language", "translation_requirement"),
    "arabic": ("approved_content", "unchanged_checked_content"),
    "proofreading": ("draft", "proofreading_result"),
    "design": ("approved_content", "attempt_render_result"),
    "rotation": ("eligible_categories,rotation_memory", "selected_category"),
    "fact_source": ("did_you_know_command", "approved_content"),
    "media_verification": ("attempt_render_result,spool_bytes", "unchanged_verified_render_result"),
    "voice": ("attempt_render_result", "unchanged_audio_checked_render_result"),
    "final": ("quality_evidence,human_approval", "delivery_decision"),
    "content_repair": ("incident,repair_budget", "repair_evidence"),
}


def contract_plan(registry=None):
    if registry is None:
        registry = json.loads(
            (Path(__file__).parent / "madar/registry.json").read_text(encoding="utf-8")
        )
    agents = registry.get("agents", [])
    ids = [agent.get("id") for agent in agents]
    if len(ids) != 18 or set(ids) != set(ROLE_IDS):
        raise ValueError("Registry must contain exactly the eighteen approved identities")
    roles = []
    for agent in agents:
        role = agent["id"]
        if "snap-bot-v2" not in agent.get("applications", []):
            raise ValueError("Registry application mismatch")
        roles.append(
            {
                "id": role,
                "input_contract": INTERFACES[role][0],
                "output_contract": INTERFACES[role][1],
                "dependencies": list(DEPENDENCIES.get(role, ())),
                "binding": "EXISTING_OBSERVER"
                if role in OBSERVED
                else "LOCAL_TOOL"
                if role in LOCAL_TOOLS
                else "NOT_BOUND",
                "adapter_status": agent["adapter_status"],
                "local_rule_implementation": "SNAP_DID_YOU_KNOW_ONLY"
                if role in PASS_THROUGH
                else LOCAL_TOOLS.get(role, "NOT_DECLARED_HERE"),
                "acceptance_status": agent["acceptance_status"],
            }
        )
    plan = {
        "schema": 1,
        "app": "snap-bot-v2",
        "content_type": "did_you_know",
        "mode": "READ_ONLY_DIAGNOSTIC",
        "roles": roles,
        "automatic_repair": False,
        "delivery_authority": False,
    }
    plan["sha256"] = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return plan


def validate_payload(role, direction, payload):
    """Payload contracts enforced by the four bound owned stage adapters."""
    if role not in OBSERVED or direction not in ("input", "output"):
        raise ValueError("Unbound contract or direction")
    if role == "final":
        from .final_agent import validate_final_payload

        return validate_final_payload(direction, payload)
    kind = (
        ("command", "content")
        if role == "fact_source"
        else ("content", "arabic_content")
        if role == "arabic"
        else ("arabic_content", "arabic_content")
        if role in CONTENT_CHECKS
        else ("render", "render")
        if role in RENDER_CHECKS
        else ("content", "render")
        if role == "design"
        else ("render", "receipt")
    )[direction == "output"]
    if not isinstance(payload, dict):
        raise ValueError("Missing contract payload")
    if kind == "command":
        if payload.get("command") != "did_you_know":
            raise ValueError("Unsupported content command")
    elif kind == "content":
        from .did_you_know import validate_content

        validate_content(payload)
    elif kind == "arabic_content":
        from .arabic_agent import inspect_content
        from .did_you_know import validate_content

        validate_content(payload)
        inspect_content(payload)
    elif kind == "render":
        required = (
            "job_id",
            "attempt",
            "content_hash",
            "video_sha256",
            "artifact",
            "qa",
            "content_type",
        )
        if any(key not in payload for key in required):
            raise ValueError("Incomplete render result")
        if not isinstance(payload["job_id"], str) or not re.fullmatch(
            "[0-9a-f]{32}", payload["job_id"]
        ):
            raise ValueError("Invalid job identity")
        if (
            payload["content_type"] != "did_you_know"
            or type(payload["attempt"]) is not int
            or payload["attempt"] < 1
        ):
            raise ValueError("Invalid render binding")
        if not isinstance(payload["qa"], dict) or payload["qa"].get("result") != "PASS":
            raise ValueError("Render QA failed")
        for key in ("content_hash", "video_sha256"):
            if not isinstance(payload[key], str) or not re.fullmatch("[0-9a-f]{64}", payload[key]):
                raise ValueError("Invalid digest")
        expected = (
            f"renders/{payload['job_id']}/attempts/{payload['attempt']}/output/did-you-know.mp4"
        )
        if payload["artifact"] != expected:
            raise ValueError("Artifact attempt mismatch")
    else:
        if set(payload) != {"job_id", "stage", "attempt", "sha256", "path"}:
            raise ValueError("Incomplete stage receipt")
        if (
            payload["stage"] != "design"
            or type(payload["attempt"]) is not int
            or payload["attempt"] < 1
        ):
            raise ValueError("Invalid stage receipt")
        if not isinstance(payload["job_id"], str) or not re.fullmatch(
            "[0-9a-f]{32}", payload["job_id"]
        ):
            raise ValueError("Invalid receipt job")
        if (
            payload["path"]
            != f"renders/{payload['job_id']}/attempts/{payload['attempt']}/output/did-you-know.mp4"
        ):
            raise ValueError("Invalid receipt path")
        if not isinstance(payload["sha256"], str) or not re.fullmatch(
            "[0-9a-f]{64}", payload["sha256"]
        ):
            raise ValueError("Invalid receipt digest")
    return True


def _details(event):
    value = event["details"]
    return json.loads(value) if isinstance(value, str) else value


def project_events(job_id, attempt, events, *, now, stale_seconds=120, registry=None):
    """Caller must supply ordered, job-scoped DB events; no raw details returned."""
    if type(attempt) is not int or attempt < 1:
        raise ValueError("Invalid attempt")
    if not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("Invalid observation time")
    if (
        not isinstance(stale_seconds, (int, float))
        or not math.isfinite(stale_seconds)
        or stale_seconds <= 0
    ):
        raise ValueError("Invalid freshness window")
    plan = contract_plan(registry)
    latest = {}
    runs = {}
    for event in events:
        if event["event"] != "agent.run":
            continue
        details = _details(event)
        if type(details.get("attempt")) is not int:
            raise ValueError("Missing event attempt")
        if details["attempt"] != attempt:
            continue
        role, run, status = (details.get(key) for key in ("agent", "run", "status"))
        if role not in OBSERVED:
            raise ValueError("Unbound observed role")
        if not isinstance(run, str) or not re.fullmatch("[0-9a-f]{32}", run):
            raise ValueError("Invalid observer run")
        if status not in ("RUNNING", "SUCCEEDED", "FAILED"):
            raise ValueError("Unknown observer status")
        at = event["at"]
        if not isinstance(at, (int, float)) or not math.isfinite(at) or at > now:
            raise ValueError("Invalid event time")
        previous = runs.get(run)
        if (previous is None and status != "RUNNING") or (
            previous is not None
            and (status == "RUNNING" or previous[0] != role or previous[1] != "RUNNING")
        ):
            raise ValueError("Invalid observer event sequence")
        if any(latest.get(dep, {}).get("status") != "SUCCEEDED" for dep in DEPENDENCIES[role]):
            raise ValueError("Observed stage before successful dependency")
        runs[run] = (role, status)
        latest[role] = {"status": status, "at": at, "run": run}
    rows = []
    for declared in plan["roles"]:
        row = {
            **declared,
            "status": "NOT_OBSERVED"
            if declared["binding"] == "EXISTING_OBSERVER"
            else declared["binding"],
            "evidence": "NO_EVENT",
            "outputs_verified": False,
            "incident": None,
        }
        if declared["id"] in latest:
            item = latest[declared["id"]]
            row.update(status=item["status"], evidence="OWNED_DB_EVENT", observed_at=item["at"])
            if item["status"] == "RUNNING" and now - item["at"] > stale_seconds:
                row["status"] = "UNKNOWN_STALE"
            if item["status"] == "FAILED":
                row["incident"] = hashlib.sha256(
                    f"snap-bot-v2:{job_id}:{attempt}:{item['run']}".encode()
                ).hexdigest()
        rows.append(row)
    by_id = {row["id"]: row for row in rows}
    for role in OBSERVED:
        row = by_id[role]
        if row["status"] != "NOT_OBSERVED":
            continue
        for dep in DEPENDENCIES[role]:
            upstream = by_id[dep]
            if upstream["status"] in ("FAILED", "BLOCKED_BY_UPSTREAM"):
                row.update(
                    status="BLOCKED_BY_UPSTREAM",
                    incident=upstream["incident"],
                    root_role=upstream.get("root_role", dep),
                )
    return {
        "schema": 1,
        "app": "snap-bot-v2",
        "job_id": job_id,
        "attempt": attempt,
        "observed_at": now,
        "plan_sha256": plan["sha256"],
        "mode": plan["mode"],
        "delivery_authority": False,
        "automatic_repair": False,
        "roles": rows,
    }


def read_snapshot(database, job_id, *, now, registry=None):
    """Existing SQLite only, mode=ro plus one read transaction; no Repository init."""
    path = Path(database).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        job = db.execute("SELECT app,command FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["app"] != "snap-bot-v2" or job["command"] != "did_you_know":
            raise ValueError("Snapshot is scoped to a Snap did_you_know job")
        events = [
            dict(row)
            for row in db.execute(
                "SELECT at,event,details FROM events WHERE job_id=? ORDER BY id", (job_id,)
            )
        ]
        claims = [_details(event) for event in events if event["event"] == "job.claimed"]
        if not claims:
            raise ValueError("No claimed attempt")
        return project_events(job_id, claims[-1].get("attempt"), events, now=now, registry=registry)
