"""Every command-path unit template in config/systemd is installed by the script."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# War alerts stay uninstalled until the owner turns them on (decision 1 Nov).
NOT_INSTALLED = {"madar-v3-snap-war-alerts", "madar-v3-status-war-alerts"}


def test_command_unit_templates_are_installed():
    spec = importlib.util.spec_from_file_location(
        "install_owner_commands", ROOT / "scripts/install_owner_commands.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    planned = {p.name for p in module.plan()}
    templates = {
        p.name
        for p in (ROOT / "config/systemd").iterdir()
        if p.stem not in NOT_INSTALLED and p.name not in planned
    }
    # Origin and tunnel are installed by their own documented activation step.
    assert templates <= {"madar-v3-whatsapp-origin.service", "madar-v3-whatsapp-tunnel.service"}
