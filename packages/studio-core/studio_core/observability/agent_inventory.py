"""Per-application binding inventory; never an execution or delivery authority.

Bindings are installation declarations, not proof of complete role duties.
Acceptance remains NOT_VERIFIED until independent acceptance is implemented.
"""

import hashlib
import json
import re
from typing import Any

APPLICATIONS = ("snap-bot-v2", "status-bot-v2")
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


def inventory(registry: Any, application: str, revision: str, bindings: Any) -> dict[str, Any]:
    """Project allowlisted installation metadata into eighteen isolated rows.

    No raw payloads, runtime outcomes, credentials or arbitrary fields are copied.
    Supplying a binding cannot mark a role as fully implemented or accepted.
    """
    if application not in APPLICATIONS:
        raise ValueError("Unknown application")
    if not isinstance(revision, str) or not re.fullmatch("[0-9a-f]{40}", revision):
        raise ValueError("Invalid source revision")
    if not isinstance(registry, dict) or not isinstance(registry.get("agents"), list):
        raise ValueError("Invalid registry")
    agents = registry["agents"]
    if (
        len(agents) != 18
        or any(not isinstance(a, dict) for a in agents)
        or any(not isinstance(a.get("id"), str) for a in agents)
        or {a["id"] for a in agents} != set(ROLE_IDS)
    ):
        raise ValueError("Exactly eighteen fixed identities required")
    for agent in agents:
        apps = agent.get("applications")
        if (
            not isinstance(apps, list)
            or len(apps) != 2
            or any(not isinstance(app, str) for app in apps)
            or set(apps) != set(APPLICATIONS)
        ):
            raise ValueError("Independent application identities required")
        if agent.get("state_isolated_per_application") is not True:
            raise ValueError("Application state must remain isolated")
        if agent.get("execution_policy") != "WHEN_APPLICABLE":
            raise ValueError("Unsupported execution policy")
    if not isinstance(bindings, dict) or set(bindings) - set(ROLE_IDS):
        raise ValueError("Unknown role binding")
    for binding in bindings.values():
        if (
            not isinstance(binding, dict)
            or set(binding) != {"scope", "kind"}
            or not isinstance(binding["kind"], str)
            or binding["kind"] not in {"OBSERVER", "LOCAL_RULE_ADAPTER"}
            or not isinstance(binding["scope"], str)
            or not re.fullmatch("[a-z][a-z0-9_]{0,63}", binding["scope"])
        ):
            raise ValueError("Invalid binding declaration")
    rows = []
    for role in ROLE_IDS:
        binding = bindings.get(role)
        rows.append(
            {
                "instance_id": application + "/" + role,
                "role_id": role,
                "binding": binding["kind"] if binding else "NOT_BOUND",
                "scope": binding["scope"] if binding else None,
                "runtime_state": "UNKNOWN",
                "function_acceptance": "NOT_VERIFIED",
            }
        )
    result = {
        "schema": 1,
        "app": application,
        "revision": revision,
        "mode": "DECLARATION_ONLY",
        "agents": rows,
        "production_ready": False,
        "delivery_authority": False,
        "automatic_repair": False,
    }
    result["sha256"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result
