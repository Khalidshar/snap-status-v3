"""Auditor gate (owner directive 18 Nov 2026): every rendered clip against one design card.

The card (config/design/card.v1.json) is pinned by its hash below; changing it is an owner
decision, never a side effect. The auditor reads what the renderer actually laid out
(layout.json «styles») and the plan, and returns a machine-readable verdict. Any error
blocks export; each error names the agent that must fix it (writer or designer).
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .model import budget

ROOT = Path(__file__).resolve().parents[4]
CARD_PATH = ROOT / "config/design/card.v1.json"
CARD_SHA256 = "3e7578a7b4600f052341c1b2f00588f1505ac6cf8350610a7ba9bf7eba6b442c"
BRAND_MANIFEST = ROOT / "assets/brand/rawwag/manifest.json"
FORBIDDEN_IN_CONTENT = re.compile(
    r"(?<![\w])(?:تابعني|تابعونا|تابعنا|تابع الحساب|شاركها|شاركه|شاركوها|شاركوه"
    r"|شارك المقطع|شاركوا المقطع|انشر المقطع)(?![\w])"
)


def _digest(path: Path) -> str:
    # Line endings normalised: the same file on a Windows checkout and on the server.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_card(path: Path = CARD_PATH, expected: str = CARD_SHA256) -> dict[str, Any]:
    raw = path.read_bytes()
    if _digest(path) != expected:
        raise ValueError("DESIGN_CARD_CHANGED")
    card: dict[str, Any] = json.loads(raw)
    if card.get("schema") != "design-card.v1":
        raise ValueError("DESIGN_CARD_SCHEMA")
    if _digest(BRAND_MANIFEST) != card["brand_manifest_sha256"]:
        raise ValueError("DESIGN_CARD_BRAND_CHANGED")
    return card


def _error(errors: list[dict[str, Any]], code: str, agent: str, **detail: Any) -> None:
    errors.append({"code": code, "agent": agent, **detail})


def _text_rule(
    errors: list[dict[str, Any]], scene: dict[str, Any], name: str, rule: dict[str, Any]
) -> None:
    seen = scene["text"].get(name)
    if seen is None:
        return
    where = {"scene": scene["index"], "element": name}
    if seen["family"] not in rule["family"]:
        _error(errors, "DESIGN_FONT_FAMILY", "designer", expected=rule["family"],
               found=seen["family"], **where)  # fmt: skip
    if seen["color"] not in rule["color"]:
        _error(errors, "DESIGN_COLOR", "designer", expected=rule["color"],
               found=seen["color"], **where)  # fmt: skip
    low, high = rule["size"]
    if not low <= seen["size"] <= high:
        _error(errors, "DESIGN_FONT_SIZE", "designer", expected=[low, high],
               found=seen["size"], **where)  # fmt: skip


def audit(p: dict[str, Any], layout: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    # Writer: the script itself.
    shortest, longest, _ = budget(p["app"], p["key"])
    if not shortest <= p["duration"] <= longest:
        _error(errors, "SCRIPT_DURATION", "writer", expected=[shortest, longest],
               found=round(p["duration"], 2))  # fmt: skip
    for index, scene in enumerate(p["scenes"]):
        body = str(scene.get("body", ""))
        if scene.get("role") != "cta" and not body.strip() and not scene.get("phrases"):
            _error(errors, "SCRIPT_EMPTY_TEXT", "writer", scene=index)
        if scene.get("role") != "cta" and FORBIDDEN_IN_CONTENT.search(body):
            _error(errors, "SCRIPT_CALL_TO_ACTION", "writer", scene=index)
    # Designer: what was actually laid out, against the card.
    styles = layout.get("styles")
    if not isinstance(styles, dict):
        _error(errors, "DESIGN_STYLES_MISSING", "designer")
    else:
        if styles.get("canvas") != card["canvas"]:
            _error(errors, "DESIGN_CANVAS", "designer", expected=card["canvas"],
                   found=styles.get("canvas"))  # fmt: skip
        if styles.get("handle") is not None:
            _text_rule(errors, {"index": -1, "text": {"handle": styles["handle"]}},
                       "handle", card["handle"])  # fmt: skip
        layouts = card["layouts"]
        for scene in styles.get("scenes", []):
            rules = layouts.get(scene["kind"])
            if rules is None:
                _error(errors, "DESIGN_LAYOUT_UNKNOWN", "designer", scene=scene["index"],
                       found=scene["kind"])  # fmt: skip
                continue
            if scene["kind"] not in card["allowed"].get(p["key"], card["allowed"]["*"]):
                _error(errors, "DESIGN_LAYOUT_NOT_ALLOWED", "designer", scene=scene["index"],
                       found=scene["kind"])  # fmt: skip
            if (
                scene["card_padding"] is not None
                and scene["card_padding"] not in rules["card_padding"]
            ):
                _error(errors, "DESIGN_PADDING", "designer", scene=scene["index"],
                       expected=rules["card_padding"], found=scene["card_padding"])  # fmt: skip
            if scene["overlaps"]:
                _error(errors, "DESIGN_TEXT_OVERLAP", "designer", scene=scene["index"],
                       found=scene["overlaps"])  # fmt: skip
            for name, rule in rules["text"].items():
                _text_rule(errors, scene, name, rule)
    passed = not errors
    return {
        "schema": "audit.v1",
        "app": p["app"],
        "key": p["key"],
        "card_sha256": CARD_SHA256,
        "checks": sorted({e["code"] for e in errors}),
        "errors": errors,
        "verdict": "PASS" if passed else "REJECT",
        "reject_to": sorted({e["agent"] for e in errors}),
        "export_allowed": passed,
    }
