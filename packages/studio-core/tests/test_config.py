from pathlib import Path

import pytest

from studio_core.config import (
    CONFIG_ENV_VAR,
    ConfigError,
    feature_enabled,
    load_config,
    resolve_config_path,
)

DEFAULTS = Path(__file__).resolve().parents[3] / "config" / "defaults.toml"


def test_defaults_load():
    assert load_config(DEFAULTS)["runtime"]["render_concurrency"] == 1


def test_auto_publish_and_schedulers_disabled_by_default():
    config = load_config(DEFAULTS)
    assert feature_enabled(config, "whatsapp_auto_publish") is False
    assert feature_enabled(config, "snap_scheduler_enabled") is False
    assert feature_enabled(config, "status_scheduler_enabled") is False


def test_unknown_flag_rejected():
    with pytest.raises(ConfigError):
        feature_enabled(load_config(DEFAULTS), "does_not_exist")


def test_env_var_selects_config(monkeypatch, tmp_path):
    target = tmp_path / "custom.toml"
    target.write_text("[features]\nsnap_enabled = false\n")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(target))
    assert resolve_config_path() == target
    assert feature_enabled(load_config(), "snap_enabled") is False


def test_discovers_defaults_from_parent_directory(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "defaults.toml").write_text("[features]\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    monkeypatch.chdir(nested)
    assert resolve_config_path() == tmp_path / "config" / "defaults.toml"


def test_missing_config_fails_closed(monkeypatch, tmp_path):
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError):
        resolve_config_path()
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")


def test_invalid_toml_rejected(tmp_path):
    broken = tmp_path / "broken.toml"
    broken.write_text("[features\n")
    with pytest.raises(ConfigError):
        load_config(broken)
