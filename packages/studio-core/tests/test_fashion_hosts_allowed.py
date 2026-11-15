import tomllib
from pathlib import Path
from urllib.parse import urlparse

import pytest

from studio_core.production.fashion_policy import KEYS
from studio_core.sources.news import news_sources

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "config", ["config/snap/news.toml", "config/status/news.toml", "config/status/commands.toml"]
)
def test_fashion_source_hosts_are_allowed_by_command_egress(config):
    """Telegram/WhatsApp fashion commands fetch through this egress; a missing host = no news."""
    allowed = set(tomllib.loads((ROOT / config).read_text("utf-8"))["network"]["hosts"])
    needed = {
        urlparse(source.url).hostname
        for key in KEYS
        for source in news_sources(key, production=True)
    }
    assert needed <= allowed, sorted(needed - allowed)
