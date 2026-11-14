import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
import test_status_whatsapp_outbox as fixtures

from status_bot import agents, content_clips, tool_commands
from studio_core import commands
from studio_core.observability import role_status
from studio_core.observability.agent_inventory import ROLE_IDS
from studio_core.observability.agent_registry import load_registry

ROOT = Path(__file__).parents[3]


def test_exactly_eighteen_identities_in_each_independent_registry(tmp_path):
    for app in ("snap-bot-v2", "status-bot-v2"):
        identities = [r["id"] for r in load_registry()["agents"]]
        assert len(identities) == len(set(identities)) == 18
        assert set(identities) == set(ROLE_IDS)
        path = tmp_path / app / "agents.sqlite"
        role_status.record(path, app, "manager", "SUCCEEDED")
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT count(*) FROM identities").fetchone()[0] == 18
        assert len(role_status.snapshot(path, app)) == 18
    assert len(agents.CHAIN) == 12 and len(agents.LOCAL_TOOLS) == 6
    assert len(set(agents.ROLES)) == 18


def test_foreign_app_ledger_refused(tmp_path):
    path = tmp_path / "agents.sqlite"
    role_status.record(path, "snap-bot-v2", "manager", "SUCCEEDED")
    with pytest.raises(ValueError, match="FOREIGN"):
        role_status.record(path, "status-bot-v2", "manager", "FAILED")


@pytest.mark.parametrize("key", list(agents.LOCAL_TOOLS))
def test_tools_are_whatsapp_commands_with_actual_outcomes(key, tmp_path):
    case = fixtures.WhatsAppContracts()
    case.setUp()
    try:
        assert commands.parse(commands.BY_KEY[key].label)["command"].key == key
        # Writer/reviewer/fixer get immutable validated source via the tool boundary.
        with (
            patch.object(
                tool_commands,
                "latest",
                return_value={"type": "did_you_know", "body": "نص عربي", "sacred": False},
            ),
            patch("status_bot.oversight.write", return_value="نص عربي"),
            patch("status_bot.oversight.review", return_value={"decision": "PASS"}),
        ):
            response = tool_commands.execute(key, case.outbox)
        assert tool_commands.NAMES[key] in response
        result = {
            r["id"]: r
            for r in role_status.snapshot(tool_commands.ledger(case.outbox), "status-bot-v2")
        }
        assert result[key]["state"] == "SUCCEEDED"
        text = tool_commands.status(case.outbox)
        assert len(text.splitlines()) == 19
    finally:
        case.doCleanups()


def test_sacred_text_fixer_is_blocked_and_recorded():
    case = fixtures.WhatsAppContracts()
    case.setUp()
    try:
        with (
            patch.object(tool_commands, "latest", return_value={"sacred": True}),
            patch("status_bot.oversight.fix") as fix,
        ):
            assert "المقفل" in tool_commands.execute("fixer", case.outbox)
            fix.assert_not_called()
        rows = {
            r["id"]: r["state"]
            for r in role_status.snapshot(tool_commands.ledger(case.outbox), "status-bot-v2")
        }
        assert rows["fixer"] == "FAILED"
    finally:
        case.doCleanups()


def test_every_content_command_routes_to_the_same_twelve_stage_builder():
    # The dispatch table covers all non-weather clips; weather has its independent chain.
    for command in commands.COMMANDS:
        if command.section in {"general", "faith"}:
            assert command.key in content_clips.KINDS
    assert content_clips.CHAIN == agents.CHAIN
