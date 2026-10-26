"""The main bot's owner command listener: owner-only, fresh, read-only answers."""

import json
import time
import unittest
from unittest.mock import patch

from snap_bot import owner_commands as listener

OWNER = "77"


def update(uid, text, chat=OWNER, sender=OWNER, kind="private", age=5, bot=False):
    return {
        "update_id": uid,
        "message": {
            "date": int(time.time()) - age,
            "text": text,
            "chat": {"id": int(chat), "type": kind},
            "from": {"id": int(sender), "is_bot": bot},
        },
    }


class FakeRuntime:
    def __init__(self, events=None):
        self.list = events or []
        self.appended = []

    def events(self):
        return list(self.list)

    def append(self, *args, **kwargs):
        self.appended.append(args)


class OwnerCommandTests(unittest.TestCase):
    def setUp(self):
        for active in (
            patch.object(listener, "source_info", return_value=("2d0191a" + "0" * 33, 1000)),
            patch.object(listener, "channel_bot", return_value="khalid_s2ss_bot"),
        ):
            active.start()
            self.addCleanup(active.stop)
        now = time.time()
        self.runtime = FakeRuntime(
            [
                {"kind": "RENDERED", "video_sha": "96a4970e" + "0" * 56, "time": now},
                {"kind": "HUMAN_FRAME", "video_sha": "96a4970e" + "0" * 56, "time": now},
                {
                    "kind": "ACK",
                    "video_sha": "96a4970e" + "0" * 56,
                    "bot": "khalid_s2ss_bot",
                    "message_id": 946,
                    "time": now,
                },
            ]
        )
        self.studio = listener.Studio(self.runtime)

    def test_only_the_owners_fresh_private_text_is_read(self):
        updates = [
            update(1, "1"),
            update(2, "1", chat="99", sender="99"),
            update(3, "1", kind="group"),
            update(4, "1", sender="99"),
            update(5, "1", bot=True),
            update(6, "1", age=listener.FRESH_SECONDS + 5),
            {"update_id": 7, "message": {"chat": {"id": 77, "type": "private"}}},
            "noise",
        ]
        cursor, texts = listener.owner_messages(updates, OWNER, time.time())
        self.assertEqual(texts, ["1"], "guard:owner-only")
        self.assertEqual(cursor, 8)

    def test_read_only_answers(self):
        self.assertIn("khalid_s2ss_bot", self.studio.answer("الحالة"))
        self.assertIn("2d0191a", self.studio.answer("الحالة"))
        self.assertIn("رسالة 946", self.studio.answer("آخر مقطع"))
        self.assertIn("غير مفعّلة", self.studio.answer("الحالة"))
        self.assertIn("إرسال: 1", self.studio.answer("تقرير اليوم"))
        self.assertIn("أدوات محلية", self.studio.answer("حالة الوكلاء"))
        self.assertIn("لا توجد جدولة", self.studio.answer("مواعيد الجدولة"))

    def test_commands_with_effects_do_nothing_yet(self):
        for text in ("هل تعلم", "👁 معاينة", "✅ أرسل", "🛑 إيقاف كامل", "أضف هل تعلم: نص", "نعم"):
            with patch.object(listener.menu, "parse", wraps=listener.menu.parse):
                answer = self.studio.answer(text)
            self.assertTrue("لم يصل إلى v3" in answer or "لا يوجد أمر" in answer, text)
        self.assertEqual(self.runtime.appended, [], "guard:read-only")

    def test_unknown_text_shows_the_menu(self):
        self.assertIn("القائمة", self.studio.answer("لا توقف شي"))
        self.assertIn("⏯ التحكم", self.studio.answer("لا توقف شي"))

    def test_poll_replies_only_to_the_owner_and_advances(self):
        calls = []

        def transport(token, method, body=b""):
            calls.append((method, json.loads(body or b"{}")))
            if method == "getUpdates":
                return [update(10, "الحالة"), update(11, "1", chat="99", sender="99")]
            return {"message_id": 1, "chat": {"id": 77}}

        offset = listener.poll_once("t", OWNER, self.studio, 3, transport)
        self.assertEqual(offset, 12)
        sends = [body for method, body in calls if method == "sendMessage"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["chat_id"], OWNER, "guard:reply-owner")
        self.assertEqual(calls[0][1]["offset"], 3)
        self.assertEqual({m for m, _ in calls}, {"getUpdates", "sendMessage"})

    def test_credentials_come_from_the_main_channel_only(self):
        with patch.dict("os.environ", {"TG_TOKEN": "", "TG_CHAT": "77"}, clear=False):
            with self.assertRaises(ValueError):
                listener.credentials()
        with patch.dict(
            "os.environ", {"TG_TOKEN": "654321:" + "y" * 35, "TG_CHAT": "77"}, clear=False
        ):
            self.assertEqual(listener.credentials()[2], "khalid_s2ss_bot")


if __name__ == "__main__":
    unittest.main()
