import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

from snap_bot import owner_commands as listener
from studio_core.commands import BY_KEY


def update(uid, key, age=0):
    return {
        "update_id": uid,
        "message": {
            "date": int(time.time()) - age,
            "text": BY_KEY[key].label,
            "chat": {"id": 77, "type": "private"},
            "from": {"id": 77},
        },
    }


def run(updates, monkeypatch, tmp_path):
    monkeypatch.setenv("MADAR_LEGACY_PRODUCTION", "1")
    monkeypatch.setattr(listener, "HALT", tmp_path / "HALT")
    studio = SimpleNamespace(make=Mock(return_value=""), runtime=Mock())
    texts = []

    def transport(token, method, body=b"", kind=None):
        if method == "sendMessage":
            texts.append(json.loads(body)["text"])
        return updates if method == "getUpdates" else {}

    listener.poll_once("token", "77", studio, 0, transport)
    return studio, texts


def test_every_queued_clip_command_is_acknowledged_at_once(monkeypatch, tmp_path):
    studio, texts = run(
        [update(1, "verse"), update(2, "dua"), update(3, "adhkar_am")], monkeypatch, tmp_path
    )
    assert [c.args[0] for c in studio.make.call_args_list] == ["verse", "dua", "adhkar_am"]
    receipts = [t for t in texts if t.startswith("📥")]
    assert len(receipts) == 2 and "١ في الدور" in receipts[0] and "٢ في الدور" in receipts[1]
    # Both receipts go out before the first clip starts being made.
    assert texts.index(receipts[1]) < next(i for i, t in enumerate(texts) if t.startswith("⏳"))


def test_a_command_too_old_on_arrival_says_so_instead_of_vanishing(monkeypatch, tmp_path):
    studio, texts = run(
        [update(1, "verse", age=listener.FRESH_SECONDS + 60)], monkeypatch, tmp_path
    )
    studio.make.assert_not_called()
    assert len(texts) == 1 and texts[0].startswith("⌛") and "أرسله من جديد" in texts[0]


def test_age_is_judged_on_arrival_not_when_its_turn_comes(monkeypatch, tmp_path):
    clock = {"now": time.time()}
    monkeypatch.setattr(listener.time, "time", lambda: clock["now"])
    studio = SimpleNamespace(runtime=Mock())

    def slow(kind):
        clock["now"] += listener.FRESH_SECONDS + 300  # a long clip ahead in the queue
        return ""

    studio.make = Mock(side_effect=slow)
    first, second = update(1, "verse"), update(2, "dua")
    for item in (first, second):
        item["message"]["date"] = int(clock["now"])
    monkeypatch.setenv("MADAR_LEGACY_PRODUCTION", "1")
    monkeypatch.setattr(listener, "HALT", tmp_path / "HALT")

    def transport(token, method, body=b"", kind=None):
        return [first, second] if method == "getUpdates" else {}

    listener.poll_once("token", "77", studio, 0, transport)
    assert [c.args[0] for c in studio.make.call_args_list] == ["verse", "dua"]
