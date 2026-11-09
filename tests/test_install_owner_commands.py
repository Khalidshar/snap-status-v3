"""Every command-path unit template in config/systemd is installed by the script."""

import importlib.util
from pathlib import Path

import pytest

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
    # These units have separate installation steps in their deployment guides.
    assert templates <= {
        "madar-v3-whatsapp-origin.service",
        "madar-v3-whatsapp-tunnel.service",
        "madar-v3-panel.service",
    }


def test_units_follow_the_release_owner(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "install_owner_commands", ROOT / "scripts/install_owner_commands.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    default = module.plan()
    assert module.owner() == "ubuntu"
    assert any("Group=ubuntu\n" in text for text in default.values())
    import os
    import pwd

    uid = os.getuid() or 1
    (tmp_path / "madar-release.json").write_text(f'{{"owner_uid": {uid}}}')
    monkeypatch.setattr(module, "ROOT", tmp_path)
    assert module.owner() == pwd.getpwuid(uid).pw_name


@pytest.fixture
def installer(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "install_owner_commands", ROOT / "scripts/install_owner_commands.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "UNITS", tmp_path / "systemd")
    monkeypatch.setattr(module, "BACKUPS", tmp_path / "backups")
    return module


def test_all_five_released_memory_units_are_backed_up_and_replaced(installer):
    names = (
        "madar-v3-snap-brief@.service",
        "madar-v3-snap-quran-render@.service",
        "madar-v3-status-commands.service.d/memory.conf",
        "madar-v3-status-content@.service",
        "madar-v3-status-quran-render@.service",
    )
    plan = installer.plan()
    files = {installer.UNITS / name: plan[installer.UNITS / name] for name in names}
    originals = {}
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        originals[path] = content.replace("MemoryMax=3G", "MemoryMax=1536M").encode()
        path.write_bytes(originals[path])
    changes = installer.unit_changes(files)
    assert changes == originals
    backup = installer.backup_units(changes)
    assert backup is not None and backup.stat().st_mode & 0o077 == 0
    for path, old in changes.items():
        saved = backup / path.relative_to(installer.UNITS)
        assert saved.read_bytes() == old == path.read_bytes()
        installer.install_unit(path, files[path].encode(), old)
        assert path.read_bytes() == files[path].encode()
    assert installer.unit_changes(files) == {}
    assert installer.backup_units({}) is None


def test_preflight_refuses_other_bytes_even_if_recognized_for_another_unit(installer):
    files = installer.plan()
    brief = installer.UNITS / "madar-v3-snap-brief@.service"
    status = installer.UNITS / "madar-v3-status-content@.service"
    brief.parent.mkdir()
    old_brief = files[brief].replace("MemoryMax=3G", "MemoryMax=1536M").encode()
    brief.write_bytes(old_brief)
    status.write_bytes(old_brief)
    with pytest.raises(ValueError, match="Existing different unit refused"):
        installer.unit_changes(files)
    assert brief.read_bytes() == old_brief
    assert not installer.BACKUPS.exists()


def test_install_does_not_overwrite_a_unit_changed_after_preflight(installer):
    files = installer.plan()
    path = installer.UNITS / "madar-v3-snap-brief@.service"
    path.parent.mkdir()
    path.write_text(files[path].replace("MemoryMax=3G", "MemoryMax=1536M"))
    old = installer.unit_changes(files)[path]
    path.write_text("local edit")
    with pytest.raises(ValueError, match="Unit changed during install"):
        installer.install_unit(path, files[path].encode(), old)
    assert path.read_text() == "local edit"
