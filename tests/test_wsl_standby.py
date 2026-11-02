import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location(
    "wsl_standby", Path(__file__).parents[1] / "scripts/wsl_standby.py"
)
wsl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wsl)


def test_check_never_starts():
    with patch.object(wsl, "prerequisite"), patch.object(wsl, "systemctl") as ctl:
        assert wsl.operate("snap", "check")["started"] == []
        ctl.assert_not_called()


def test_primary_confirmation_required(tmp_path):
    with (
        patch.object(wsl, "ROOT", tmp_path),
        patch.object(wsl, "prerequisite"),
        patch.object(wsl.os, "geteuid", return_value=0),
    ):
        with pytest.raises(ValueError, match="تأكيد"):
            wsl.operate("snap", "start")
        assert not list(tmp_path.iterdir())


def test_failed_start_retains_ownership_and_stop_preserves_audit(tmp_path):
    state = tmp_path / "snap"
    state.mkdir()
    calls = []

    def ctl(*args):
        calls.append(args)
        return args[0] == "stop"

    with (
        patch.object(wsl, "ROOT", tmp_path),
        patch.object(wsl, "prerequisite"),
        patch.object(wsl.os, "geteuid", return_value=0),
        patch.object(wsl, "systemctl", side_effect=ctl),
    ):
        with pytest.raises(ValueError, match="فشل التشغيل"):
            wsl.operate("snap", "start", True)
        owned = json.loads((state / "standby-started.json").read_text())
        assert owned == [wsl.UNITS["snap"][0]]
        before = (state / "standby-audit.jsonl").read_text()
        assert wsl.operate("snap", "stop")["stopped"] == owned
        assert (state / "standby-audit.jsonl").read_text().startswith(before)
        assert not any(a[0] in {"enable", "restart"} for a in calls)


def test_foreign_unit_in_record_is_never_stopped(tmp_path):
    state = tmp_path / "status"
    state.mkdir()
    (state / "standby-started.json").write_text('["status-bot.timer"]')
    with (
        patch.object(wsl, "ROOT", tmp_path),
        patch.object(wsl.os, "geteuid", return_value=0),
        patch.object(wsl, "systemctl") as ctl,
    ):
        with pytest.raises(ValueError, match="غير صالح"):
            wsl.operate("status", "stop")
        ctl.assert_not_called()
