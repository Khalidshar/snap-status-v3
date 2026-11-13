"""One role catalogue for both applications and their seven display stations.

Declarations describe existing local checks, not AI or successful execution.
Application ledgers and delivery authority remain outside this module.
"""

import json
import math
from pathlib import Path
from typing import Any

_PATH = Path(__file__).with_name("agent_registry.json")
APPLICATIONS = ("snap-bot-v2", "status-bot-v2")


def load_registry() -> dict[str, Any]:
    value: dict[str, Any] = json.loads(_PATH.read_text(encoding="utf-8"))
    agents, stations = value["agents"], value["stations"]
    if (
        len(agents) != 18
        or len({a["id"] for a in agents}) != 18
        or len(stations) != 7
        or len({s["id"] for s in stations}) != 7
        or {a["station"] for a in agents} != {s["id"] for s in stations}
    ):
        raise ValueError("INVALID_AGENT_REGISTRY")
    return value


_REGISTRY = load_registry()
ROLE_IDS = tuple(a["id"] for a in _REGISTRY["agents"])
STATIONS = tuple(_REGISTRY["stations"])
CHAIN = tuple(
    a["id"] for a in sorted(_REGISTRY["agents"], key=lambda a: a["step"] or 99) if a["chain"]
)
TOOL_IDS = tuple(
    a["id"]
    for a in sorted(_REGISTRY["agents"], key=lambda a: a["tool_order"] or 99)
    if not a["chain"]
)


def panel_roles() -> list[dict[str, Any]]:
    return sorted(
        load_registry()["agents"],
        key=lambda a: (not a["chain"], a["step"] or a["tool_order"]),
    )


def command_binding(application: str, role: str) -> str | None:
    if application not in APPLICATIONS or role not in ROLE_IDS:
        raise ValueError("INVALID_AGENT_IDENTITY")
    agent = next(a for a in load_registry()["agents"] if a["id"] == role)
    result: str | None = agent["bindings"][application]["command"]
    return result


def project(application: str, observed: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if application not in APPLICATIONS:
        raise ValueError("INVALID_APP")
    result = []
    for agent in panel_roles():
        binding = agent["bindings"][application]
        bound = bool(binding["pipeline"])
        event = observed.get(agent["id"], {}) if bound else {}
        state = event.get("state", "UNKNOWN")
        if state not in {"PASS", "FAIL", "RUNNING", "STARTED", "SUCCEEDED", "FAILED"}:
            state = "UNKNOWN"
        at = event.get("at")
        if type(at) not in (int, float) or not math.isfinite(at) or at < 0:
            at = None
        result.append(
            {
                **agent,
                "instance_id": application + "/" + agent["id"],
                "binding": "LOCAL_RULE_ADAPTER" if bound else "NOT_BOUND",
                "binding_label": "فحص محلي مربوط" if bound else "غير مربوط",
                "command_binding": "BOUND" if binding["command"] else "NOT_BOUND",
                "command_label": "مربوط" if binding["command"] else "غير مربوط",
                "scope": binding["scope"],
                "accepted": False,
                "state": state if bound else "NOT_BOUND",
                "at": at,
            }
        )
    return result


def status_text(application: str, observed: dict[str, dict[str, Any]]) -> str:
    words = {
        "PASS": "اجتاز", "SUCCEEDED": "نجح", "FAIL": "فشل", "FAILED": "فشل",
        "RUNNING": "يعمل الآن", "STARTED": "يعمل الآن", "UNKNOWN": "لم يُرصد",
        "NOT_BOUND": "غير مربوط",
    }  # fmt: skip
    rows = project(application, observed)
    return "حالة الوكلاء — ١٨ دورًا في سبع محطات\n" + "\n".join(
        a["name"]
        + ": "
        + words[a["state"]]
        + (" · أداة الأوامر: " + a["command_label"] if not a["chain"] else "")
        for a in rows
    )
