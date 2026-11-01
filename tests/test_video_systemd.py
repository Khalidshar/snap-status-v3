"""Pure installer tests: no secrets, service starts or writes to the real machine."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location(
    "install_owner_commands", Path(__file__).parents[1] / "scripts/install_owner_commands.py"
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def test_video_launch_contract_and_permissions():
    unit = installer.plan()[installer.UNITS / "madar-v3-snap-video.service"]
    assert "EnvironmentFile=/etc/madar/media.env\n" in unit
    assert (
        "ExecStart=/opt/madar/current/.venv/bin/python -m studio_core.network.video "
        "--socket /run/madar/snap-video.sock\n" in unit
    )
    for directive in (
        "User=root",
        "UMask=0077",
        "RuntimeDirectory=madar",
        "RuntimeDirectoryMode=0750",
        "Group=ubuntu",
        "RuntimeDirectoryPreserve=yes",
        "ReadWritePaths=/run/madar",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "CapabilityBoundingSet=",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "Restart=no",
        "MemoryMax=1G",
    ):
        assert directive + "\n" in unit
    assert "[Install]" not in unit
    assert "status-whatsapp.env" not in unit
    assert "ReadWritePaths=/var/lib" not in unit
    assert "PrivateNetwork=yes" not in unit


def test_media_environment_is_not_shared_with_other_services():
    for path, content in installer.plan().items():
        if path.name != "madar-v3-snap-video.service":
            assert "media.env" not in content
            assert "studio_core.network.video" not in content


def test_default_only_prints_plan(tmp_path, capsys):
    with (
        patch.object(installer, "UNITS", tmp_path / "units"),
        patch.object(sys, "argv", ["install_owner_commands.py"]),
        patch.object(Path, "open", side_effect=AssertionError("No file content reads or writes")),
    ):
        installer.main()
    value = json.loads(capsys.readouterr().out)
    assert value["applied"] is False
    assert value["started_services"] == []
    assert str(tmp_path / "units/madar-v3-snap-video.service") in value["files"]
    assert not (tmp_path / "units").exists()


def test_apply_rejects_missing_gateway_before_any_writes(tmp_path):
    root = tmp_path / "release"
    root.mkdir()
    (root / "madar-release.json").write_text(json.dumps({"commit": "a" * 40}))
    with (
        patch.object(installer, "ROOT", root),
        patch.object(installer.os, "geteuid", return_value=0),
        patch.object(Path, "resolve", return_value=root),
        patch.object(sys, "argv", ["install_owner_commands.py", "--apply"]),
        patch.object(Path, "mkdir", side_effect=AssertionError("Must validate before writing")),
        pytest.raises(ValueError, match="Video gateway module missing"),
    ):
        installer.main()


def test_apply_never_reads_media_environment_or_runs_a_process():
    # Installer has no execution primitive. All runtime work is unit text, not invoked here.
    import ast

    tree = ast.parse(Path(installer.__file__).read_text())
    calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert not any(name.startswith(("subprocess.", "os.system", "os.popen")) for name in calls)
    assert not any(name.startswith("MEDIA_ENV.") for name in calls)
