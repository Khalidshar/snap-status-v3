import json
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from snap_bot import owner_commands as listener
from studio_core.commands import BY_KEY
from studio_core.production.fashion_policy import KEYS


@pytest.mark.parametrize("key", sorted(KEYS))
def test_telegram_fashion_uses_production_with_the_global_switch_off(tmp_path, monkeypatch, key):
    monkeypatch.delenv("MADAR_LEGACY_PRODUCTION", raising=False)
    monkeypatch.setenv("MADAR_LIVE_NEWS", "1")
    monkeypatch.setattr(listener, "HALT", tmp_path / "HALT")
    studio = SimpleNamespace(make=Mock(return_value=""), runtime=Mock())
    update = {
        "update_id": 17,
        "message": {
            "date": int(time.time()),
            "text": BY_KEY[key].label,
            "chat": {"id": 77, "type": "private"},
            "from": {"id": 77},
        },
    }
    calls = []

    def transport(token, method, body=b"", kind=None):
        calls.append(method)
        if body:
            json.loads(body)
        return [update] if method == "getUpdates" else {}

    with patch.object(listener.command_news, "reply") as headline_reply:
        listener.poll_once("token", "77", studio, 0, transport)
    studio.make.assert_called_once_with(key)
    headline_reply.assert_not_called()
    assert calls == ["getUpdates"]
