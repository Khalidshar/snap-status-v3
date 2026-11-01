"""The 24-hour boundary covers all replies, verified inbound refresh and status evidence."""

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import test_status_whatsapp_outbox as fixtures

from status_bot import service_window
from status_bot.owner_commands import answer, process_one


@pytest.fixture
def fixture():
    case = fixtures.WhatsAppContracts()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


@pytest.mark.parametrize(
    "age,opened", [(0, True), (86399, True), (86400, False), (86401, False), (-1, False)]
)
def test_exact_window_boundary_and_future_messages(fixture, age, opened):
    f = fixture
    now = time.time()
    f.repo.clock = lambda: now
    with f.repo.transaction() as db:
        db.execute(
            "INSERT OR REPLACE INTO whatsapp_windows VALUES(?,?)", (f.identity.binding, now - age)
        )
    value = service_window.status(f.repo, f.identity.binding)
    assert value["open"] is opened
    assert value["expires_at"] == now - age + 86400


def inbound(f, uid, body="الحالة"):
    raw, _ = f.webhook(inbound=True)
    payload = json.loads(raw)
    item = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    item.update(id="wamid." + uid, timestamp=str(int(time.time())))
    item["text"]["body"] = body
    raw = json.dumps(payload, ensure_ascii=False).encode()
    signature = "sha256=" + hmac.new(f.identity.webhook_key, raw, hashlib.sha256).hexdigest()
    f.outbox.webhook(raw, signature)


def test_verified_command_refreshes_window_and_status_shows_expiry(fixture):
    f = fixture
    assert service_window.status(f.repo, f.identity.binding)["open"] is False
    inbound(f, "REFRESH")
    assert service_window.status(f.repo, f.identity.binding)["open"] is True
    value = answer("الحالة", SimpleNamespace(events=lambda: []), outbox=f.outbox)
    assert "مفتوحة" in value and "تنتهي" in value
    assert f.identity.recipient not in value


def test_closed_window_sends_nothing_and_retains_clear_reason(fixture, capsys):
    f = fixture
    inbound(f, "CLOSED")
    with f.repo.transaction() as db:
        db.execute("UPDATE whatsapp_windows SET last_inbound_at=?", (time.time() - 86401,))
    transport = SimpleNamespace(send_text=Mock(), send_video=Mock())
    assert process_one(f.outbox, transport, None, SimpleNamespace(events=lambda: []))
    transport.send_text.assert_not_called()
    transport.send_video.assert_not_called()
    with f.repo.connect() as db:
        row = db.execute("SELECT state,code FROM whatsapp_commands").fetchone()
    assert row["code"] == "WA_SERVICE_WINDOW_CLOSED"
    assert "WA_SERVICE_WINDOW_CLOSED" in capsys.readouterr().err
    assert "مقفلة" in answer("الحالة", SimpleNamespace(events=lambda: []), outbox=f.outbox)
