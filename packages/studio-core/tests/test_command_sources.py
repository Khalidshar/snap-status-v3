"""Source inventory must never turn candidate or sacred references into delivery."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from studio_core.commands import COMMANDS
from studio_core.network.client import NetworkError, NetworkResponse
from studio_core.sources import CATALOG, CONTENT_KEYS, FAITH_KEYS, for_command, load, plan
from studio_core.sources.audit import audit


def test_all_existing_commands_are_inspectable_and_controls_have_no_sources():
    for command in COMMANDS:
        result = plan(command.key)
        assert result["number"] == command.number
        assert bool(result["sources"]) == (command.key in CONTENT_KEYS)
        assert result["publication_enabled"] is False
        assert result["live_feeds_enabled"] is False
    assert len(CONTENT_KEYS) == 21


@pytest.mark.parametrize("name", ["style", "الأناقة", "٩", "9", "👗 الأناقة والجمال"])
def test_sources_follow_the_bots_existing_exact_command_matching(name):
    assert for_command(name) == for_command("style")
    assert for_command(name) != for_command("tech")


@pytest.mark.parametrize("name", ["أحتاج الأناقة", "أخبار", "لا توقف شي", "37", ""])
def test_unknown_sentences_and_section_titles_are_never_guessed(name):
    with pytest.raises(ValueError):
        plan(name)


def test_religion_has_locked_references_and_human_audio_no_editorial_feeds():
    for key in FAITH_KEYS:
        result = plan(key)
        assert result["sacred_lock_required"] and result["human_audio_required"]
        assert all(s["kind"] in {"catalog", "reference", "human_audio"} for s in result["sources"])


@pytest.mark.parametrize(
    "mutation", ["sacred_feed", "unknown_command", "promote", "duplicate", "unsafe_url"]
)
def test_inventory_rejects_dangerous_or_incomplete_bindings(tmp_path, mutation):
    data = json.loads(CATALOG.read_text())
    first = data["sources"][0]
    if mutation == "sacred_feed":
        first["commands"].append("hadith")
    elif mutation == "unknown_command":
        first["commands"].append("new_unreviewed_command")
    elif mutation == "promote":
        first["status"] = "reference"
    elif mutation == "duplicate":
        data["sources"].append(first)
    else:
        first["url"] = "https://user:secret@example.com/rss"
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load(path)


XML = b"""<rss><channel><item><title>Headline</title><link>https://example.com/one</link>
<pubDate>Fri, 30 Oct 2026 02:00:00 GMT</pubDate></item></channel></rss>"""


class FakeClient:
    def __init__(self, body=XML, *, error=None, digest=None, status=200):
        self.body, self.error, self.status = body, error, status
        self.digest = digest if digest is not None else hashlib.sha256(body).hexdigest()
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if self.error:
            raise self.error
        return NetworkResponse(self.status, "text/xml", self.body, self.digest)


def test_audit_reads_each_news_candidate_once_without_returning_headlines():
    client = FakeClient()
    checked = audit("موجز", client)
    assert len(checked) == len(client.calls) == 10
    assert all(c["result"] == "XML_VERIFIED" and c["valid_items"] == 1 for c in checked)
    assert "Headline" not in json.dumps(checked)
    assert all(c["publication_enabled"] is False for c in checked)
    assert all(s.status == "candidate" for s in for_command("news"))


def test_audit_does_not_fetch_excluded_sources_or_turn_websites_into_feeds():
    client = FakeClient()
    checks = audit("style", client)
    by_id = {source.id: source for source in for_command("style")}
    assert all(
        by_id[c["source_id"]].url not in client.calls
        for c in checks
        if c["result"] in {"EXCLUDED", "NOT_A_FEED"}
    )
    assert not audit("hadith", client)
    assert not audit("halt", client)


@pytest.mark.parametrize(
    "client",
    [
        FakeClient(body=b"<html/>"),
        FakeClient(body=b'<!DOCTYPE rss [<!ENTITY x "evil">]><rss/>'),
        FakeClient(error=NetworkError("private-token-do-not-print")),
        FakeClient(digest="0" * 64),
        FakeClient(status=403),
    ],
)
def test_feed_failures_are_sanitized_and_never_retried(client):
    checks = audit("tech", client)
    assert len(checks) == len(client.calls) == 2
    assert all(check["result"] == "UNVERIFIED" for check in checks)
    assert "private-token" not in json.dumps(checks)


@pytest.mark.parametrize("package", ["snap_bot", "status_bot"])
def test_both_app_clis_resolve_identical_sources_without_network(package, capsys):
    from importlib import import_module

    main = import_module(package + ".cli").main
    with patch("socket.socket", side_effect=AssertionError("Inventory must be offline")):
        assert main(["sources", "--command", "الأناقة"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["plans"] == [plan("style")]
    assert output["external_delivery"] is False


def test_source_check_rejects_other_app_network_config_before_contact(tmp_path, capsys):
    from snap_bot.cli import main
    from studio_core.config import Config

    # Existing foundation fixture has the full validated settings contract.
    fixture = Path(__file__).with_name("fixtures") / "foundation.toml"
    config = replace(Config.load(fixture), app="status-bot-v2")
    with (
        patch.object(Config, "load", return_value=config),
        patch(
            "studio_core.network.client.NetworkClient.get", side_effect=AssertionError("No fetch")
        ),
    ):
        assert main(["source-check", "--command", "tech", "--network-config", str(fixture)]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


@pytest.mark.parametrize("package", ["snap_bot", "status_bot"])
@pytest.mark.parametrize("reachable", [True, False])
def test_source_check_cli_does_not_report_failed_feeds_as_success(package, reachable, capsys):
    from importlib import import_module

    from studio_core.config import Config

    main = import_module(package + ".cli").main
    fixture = Path(__file__).with_name("fixtures") / "foundation.toml"
    app = package.replace("_", "-") + "-v2"
    config = replace(Config.load(fixture), app=app)
    fake = FakeClient(error=None if reachable else NetworkError("private-token"))
    with (
        patch.object(Config, "load", return_value=config),
        patch("studio_core.network.client.NetworkClient.get", side_effect=fake.get),
    ):
        code = main(["source-check", "--command", "tech", "--network-config", str(fixture)])
    output = json.loads(capsys.readouterr().out)
    assert code == (0 if reachable else 2)
    assert output["ok"] is reachable
    assert output["external_delivery"] is False
    assert len(fake.calls) == 2
    assert "private-token" not in json.dumps(output)
