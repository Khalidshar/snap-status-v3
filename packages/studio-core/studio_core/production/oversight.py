"""Six source oversight roles, recorded alongside the twelve existing clip agents."""

import re
import time
from pathlib import Path
from typing import Any

from studio_core.jobs import Repository
from studio_core.observability.agent_runs import AgentContract, digest, run_agent

from .model import validate

ROLES = ("manager", "analyst", "writer", "reviewer", "fixer", "content_repair")
PLAN = digest({"version": 1, "roles": ROLES, "source_rewriting": False, "max_rebuilds": 1})


def inspect(role: str, value: dict[str, Any], assets: Path) -> dict[str, Any]:
    plan = validate(value, assets=assets)
    if role not in ROLES:
        raise ValueError("PRODUCTION_OVERSIGHT_ROLE_INVALID")
    if role == "analyst":
        for scene in plan["scenes"]:
            if scene.get("role") in {"story", "point", "headline", "fact", "funny"}:
                chars = (
                    len(scene["body"]) + len(scene.get("head", "")) + len(scene.get("punch", ""))
                )
                if chars / scene["seconds"] > 17.05:
                    raise ValueError("PRODUCTION_READING_BUDGET_EXCEEDED")
    if role == "writer":
        for scene in plan["scenes"]:
            if not re.search("[ء-ي]", scene["body"]):
                raise ValueError("PRODUCTION_ARABIC_REQUIRED")
    if role == "content_repair" and value["expires_at"] <= time.time():
        raise ValueError("PRODUCTION_SOURCE_EXPIRED")
    return {
        "role": role,
        "decision": "PASS",
        "content_hash": value["content_hash"],
        "source_rewriting": False,
        "max_rebuilds": 1,
        "scenes": len(plan["scenes"]),
    }


def execute(
    repo: Repository, job: str, token: str, attempt: int, value: dict[str, Any], assets: Path
) -> None:
    """Independent role contracts; source bytes are never edited, including sacred text."""
    for role in ROLES:
        contract = AgentContract(repo.config.app, "did_you_know", role, "1.0.0", PLAN, (), 30)

        def check(role: str = role) -> dict[str, Any]:
            return inspect(role, value, assets)

        def output(result: Any, payload: Any, role: str = role) -> bool:
            return bool(result == inspect(role, payload, assets))

        run_agent(
            repo,
            job,
            token,
            attempt,
            contract,
            value,
            check,
            lambda payload: isinstance(payload, dict),
            output,
            bind_dependency=False,
        )
