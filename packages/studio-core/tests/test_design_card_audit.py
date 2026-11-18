import copy
import json
from pathlib import Path

import pytest

from studio_core.production import audit as auditor

CARD = auditor.load_card()


def plan(key="world", app="snap-bot-v2", duration=65.0, body="خبر عربي كامل"):
    return {
        "app": app,
        "key": key,
        "duration": duration,
        "scenes": [{"role": "story", "body": body}, {"role": "cta", "body": ""}],
    }


def styles(kind="news_desk", **change):
    scene = {
        "index": 0,
        "kind": kind,
        "role": "story",
        "text": {
            "dhead": {"family": "readex", "size": 77.5, "color": "rgb(255, 255, 255)"},
            "dbody": {"family": "readex", "size": 46, "color": "rgb(233, 226, 207)"},
            "dsrc": {"family": "readex", "size": 36, "color": "rgb(11, 61, 46)"},
        },
        "card_padding": [0, 0, 0, 0],
        "overlaps": 0,
    }
    for name, value in change.items():
        if name in scene["text"]:
            scene["text"][name] = {**scene["text"][name], **value}
        else:
            scene[name] = value
    return {
        "styles": {
            "canvas": [1080, 1920],
            "handle": {"family": "naskh", "size": 58, "color": "rgb(255, 255, 255)"},
            "scenes": [scene],
        }
    }


def test_the_card_is_pinned_and_matches_the_brand_frame():
    assert CARD["name"] == "هوية روّق مع خالد"
    with pytest.raises(ValueError, match="DESIGN_CARD_CHANGED"):
        auditor.load_card(expected="0" * 64)


def test_an_identity_clip_passes_and_may_export():
    verdict = auditor.audit(plan(), styles(), CARD)
    assert verdict["verdict"] == "PASS" and verdict["export_allowed"] is True
    assert verdict["schema"] == "audit.v1" and verdict["errors"] == []


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"dbody": {"family": "Arial"}}, "DESIGN_FONT_FAMILY"),
        ({"dbody": {"color": "rgb(255, 0, 0)"}}, "DESIGN_COLOR"),
        ({"dbody": {"size": 40}}, "DESIGN_FONT_SIZE"),
        ({"card_padding": [10, 10, 10, 10]}, "DESIGN_PADDING"),
        ({"overlaps": 1}, "DESIGN_TEXT_OVERLAP"),
    ],
)
def test_any_departure_from_the_card_is_the_designers_and_blocks_export(change, code):
    verdict = auditor.audit(plan(), styles(**change), CARD)
    assert verdict["verdict"] == "REJECT" and verdict["export_allowed"] is False
    assert code in verdict["checks"] and verdict["reject_to"] == ["designer"]


def test_a_layout_outside_the_commands_approved_looks_is_rejected():
    verdict = auditor.audit(plan(key="facts"), styles(), CARD)
    assert "DESIGN_LAYOUT_NOT_ALLOWED" in verdict["checks"]


def test_script_errors_go_back_to_the_writer():
    verdict = auditor.audit(plan(duration=40.0, body="تابعني"), styles(), CARD)
    assert {"SCRIPT_DURATION", "SCRIPT_CALL_TO_ACTION"} <= set(verdict["checks"])
    assert verdict["reject_to"] == ["writer"]


def test_missing_layout_record_blocks_export():
    verdict = auditor.audit(plan(), {}, CARD)
    assert verdict["checks"] == ["DESIGN_STYLES_MISSING"]


def test_the_baseline_is_the_approved_identity_of_both_bots():
    card = json.loads(Path(auditor.CARD_PATH).read_text("utf-8"))
    assert card["palette"]["green"] == "#0B3D2E" and card["palette"]["gold"] == "#D6B26C"
    assert set(card["layouts"]) == {"plain", "tilawa", "news_desk", "fashion_desk", "cta"}
    mutated = copy.deepcopy(card)
    mutated["palette"]["green"] = "#000000"
    assert json.dumps(mutated) != json.dumps(card)
