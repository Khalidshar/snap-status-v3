import json
from pathlib import Path

from snap_bot.cli import main

DEFAULTS = Path(__file__).resolve().parents[3] / "config" / "defaults.toml"


def _run(capsys, *argv: str) -> tuple[int, dict]:
    code = main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_healthcheck(capsys):
    code, out = _run(capsys, "--config", str(DEFAULTS), "healthcheck")
    assert code == 0
    assert out["app"] == "snap-bot"
    assert out["ok"] is True and out["enabled"] is True


def test_dry_run_creates_job_without_delivery(capsys):
    code, out = _run(capsys, "--config", str(DEFAULTS), "dry-run", "--requested-by", "tester")
    assert code == 0
    assert out["job"]["project"] == "snap"
    assert out["job"]["destination"] == "telegram"
    assert out["job"]["status"] == "QUEUED"


def test_kill_switch_blocks_dry_run(capsys, tmp_path):
    config = tmp_path / "off.toml"
    config.write_text("[features]\nsnap_enabled = false\n")
    code, out = _run(capsys, "--config", str(config), "dry-run")
    assert code == 3
    assert out["ok"] is False


def test_missing_config_fails_closed(capsys, tmp_path):
    code, out = _run(capsys, "--config", str(tmp_path / "missing.toml"), "healthcheck")
    assert code == 2
    assert out["ok"] is False
