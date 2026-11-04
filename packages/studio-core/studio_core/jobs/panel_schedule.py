"""Read installed legacy schedule definitions without importing or running the bots."""

import ast
import json
import re
from pathlib import Path
from typing import Any

CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
COMMAND = re.compile(r"[a-z][a-z0-9_]{0,31}")
RULE = re.compile(r"(?:radar|(?:fajr|dhuhr|asr|maghrib|isha)\+[0-9]{1,2})")


def validate(schedules: Any) -> None:
    if not isinstance(schedules, dict) or set(schedules) - {"snap", "status"}:
        raise ValueError("INVALID_SCHEDULE")
    for schedule in schedules.values():
        if (
            not isinstance(schedule, dict)
            or set(schedule) != {"paused", "slots"}
            or (schedule["paused"] is not None and type(schedule["paused"]) is not bool)
            or not isinstance(schedule["slots"], list)
            or not 1 <= len(schedule["slots"]) <= 48
        ):
            raise ValueError("INVALID_SCHEDULE")
        for slot in schedule["slots"]:
            if not isinstance(slot, list) or len(slot) != 2:
                raise ValueError("INVALID_SCHEDULE_SLOT")
            command, clock = slot
            if (
                not isinstance(command, str)
                or not COMMAND.fullmatch(command)
                or not isinstance(clock, str)
                or not (CLOCK.fullmatch(clock) or RULE.fullmatch(clock))
            ):
                raise ValueError("INVALID_SCHEDULE_SLOT")


def literals(tree: ast.Module) -> dict[str, Any]:
    values: dict[str, Any] = {}
    wanted = {"SLOTS", "RADAR_CAP", "NEWS_AT", "NIGHT_AT", "AFTER"}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in wanted:
            values[target.id] = ast.literal_eval(node.value)
        elif isinstance(target, ast.Tuple) and any(
            isinstance(item, ast.Name) and item.id == "AFTER" for item in target.elts
        ):
            values["AFTER"] = ast.literal_eval(node.value)[
                next(
                    i
                    for i, item in enumerate(target.elts)
                    if isinstance(item, ast.Name) and item.id == "AFTER"
                )
            ]
    return values


def status_slots(tree: ast.Module, values: dict[str, Any]) -> list[list[str]]:
    due = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "due"),
        None,
    )
    if due is None:
        raise ValueError("LEGACY_SCHEDULE_UNAVAILABLE")
    fixed = next(
        (
            node.iter.func.value
            for node in due.body
            if isinstance(node, ast.For)
            and isinstance(node.iter, ast.Call)
            and isinstance(node.iter.func, ast.Attribute)
            and node.iter.func.attr == "items"
            and isinstance(node.iter.func.value, ast.Dict)
        ),
        None,
    )
    if fixed is None:
        raise ValueError("LEGACY_SCHEDULE_UNAVAILABLE")
    slots = []
    for key, node in zip(fixed.keys, fixed.values, strict=True):
        if key is None:
            raise ValueError("LEGACY_SCHEDULE_UNAVAILABLE")
        hour, minute = values[node.id] if isinstance(node, ast.Name) else ast.literal_eval(node)
        slots.append([ast.literal_eval(key), f"{hour:02}:{minute:02}"])
    after = values["AFTER"]
    prayer_commands = {
        "fajr": ("adhkar_am", "friday"),
        "dhuhr": ("dua",),
        "asr": ("adhkar_pm",),
        "maghrib": ("maghrib", "fast", "kahf"),
        "isha": ("isha",),
    }
    slots.extend(
        [command, f"{prayer}+{after}"]
        for prayer, commands in prayer_commands.items()
        for command in commands
    )
    slots.append(["urgent", "radar"])
    return slots


def read(app: str, script: Path, state: Path | None) -> dict[str, Any]:
    script = script.resolve(strict=True)
    if script.name != ("snap.py" if app == "snap" else "run.py") or "studio-v2" in script.parts:
        raise ValueError("INVALID_LEGACY_SCRIPT")
    tree = ast.parse(script.read_text(encoding="utf-8"))
    values = literals(tree)
    if app == "snap":
        slots = [[command, clock] for clock, command in values["SLOTS"]]
        slots.extend([command, "radar"] for command in values["RADAR_CAP"])
    else:
        slots = status_slots(tree, values)
    paused = None
    if state is not None:
        try:
            state = state.resolve(strict=True)
            if state.suffix != ".json" or "studio-v2" in state.parts:
                raise ValueError("INVALID_LEGACY_STATE")
            flags = json.loads(state.read_bytes())
            if isinstance(flags, dict) and all(
                type(flags.get(key, False)) is bool for key in ("paused", "halted")
            ):
                paused = flags.get("paused", False) or flags.get("halted", False)
        except (OSError, ValueError):
            pass
    schedule = {"paused": paused, "slots": slots}
    validate({app: schedule})
    return schedule
