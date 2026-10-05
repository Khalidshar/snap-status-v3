import pytest

from studio_core.agents import AgentAdapter, AgentExecutionError
from studio_core.quality import Check, QualityReport, Verdict
from studio_core.security import Allowlist, UnauthorizedCommand, redact_secrets


def test_agent_adapter_records_success():
    result, run = AgentAdapter("echo", lambda value: value.upper()).run("x")
    assert result == "X"
    assert run.ok and run.agent_id == "echo" and run.output_hash


def test_agent_adapter_records_failure():
    def broken() -> None:
        raise ValueError("boom")

    with pytest.raises(AgentExecutionError) as caught:
        AgentAdapter("broken", broken).run()
    assert not caught.value.run.ok
    assert caught.value.run.error == "boom"


def test_quality_report_verdicts():
    assert QualityReport().verdict == Verdict.PASS
    warn = QualityReport([Check("a", Verdict.PASS), Check("b", Verdict.WARN)])
    assert warn.verdict == Verdict.WARN
    warn.require_publishable()
    failing = QualityReport([Check("a", Verdict.WARN), Check("b", Verdict.FAIL)])
    assert failing.verdict == Verdict.FAIL
    with pytest.raises(RuntimeError):
        failing.require_publishable()


def test_allowlist():
    allowlist = Allowlist(frozenset({"owner"}))
    allowlist.require("owner")
    with pytest.raises(UnauthorizedCommand):
        allowlist.require("stranger")


def test_redact_secrets():
    token = "123456789:" + "A" * 30
    redacted = redact_secrets(f"api_key=abc123 bot {token}")
    assert "abc123" not in redacted
    assert token not in redacted
    assert "[REDACTED_TOKEN]" in redacted
