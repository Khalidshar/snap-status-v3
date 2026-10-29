"""The main bot's owner command listener: owner-only, fresh answers and the clip cycle."""

import json
import tempfile
import time
import unittest
from pathlib import Path
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
    def __init__(self, events=None, root=None):
        self.list = events or []
        self.appended = []
        self.approved = []
        self.root = Path(root or tempfile.mkdtemp())

    def events(self):
        return list(self.list)

    def append(self, *args, **kwargs):
        self.appended.append(args)

    def approve(self, sha, channel, owner_chat=False):
        self.approved.append((sha, channel, owner_chat))
        self.list.append({"kind": "HUMAN_FRAME", "video_sha": sha, "time": time.time()})

    def rendered(
        self, n, body="نص المعلومة", expires=600, kind=None, title="هل تعلم؟", commit=None
    ):
        """A kept preview exactly as the server render leaves it."""
        sha, preview, job = f"{n:064x}", f"{n:032x}", f"{n + 9:032x}"
        directory = self.root / "previews" / preview
        (directory / "state/renders" / job / "input").mkdir(parents=True)
        (directory / "preview.json").write_text(
            json.dumps({"job_id": job, "expires_at": time.time() + expires})
        )
        (directory / "preview.png").write_bytes(b"png-bytes")
        (directory / "state/renders" / job / "input/content.json").write_text(
            json.dumps(
                {
                    "body": body,
                    "category": "body",
                    "title": title,
                    **({"kind": kind} if kind else {}),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.list.append(
            {
                "kind": "RENDERED",
                "video_sha": sha,
                "preview_id": preview,
                "time": time.time(),
                "commit": commit or listener.source_info()[0],
            }
        )
        return sha


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

    def test_commands_outside_the_clip_cycle_do_nothing_yet(self):
        for text in ("أضف هل تعلم: نص", "خبر عاجل", "⏸ إيقاف الجدولة"):
            self.assertIn("لم يصل إلى v3", self.studio.answer(text), text)
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

    def test_menu_and_start_send_the_registered_working_keyboard_offline(self):
        for text in ("القائمة", "/start"):
            calls = []

            def transport(token, method, body=b""):
                payload = json.loads(body or b"{}")
                calls.append((method, payload))
                return [update(15, text)] if method == "getUpdates" else {}

            listener.poll_once("t", OWNER, self.studio, 0, transport)
            message = calls[-1][1]
            self.assertEqual(calls[-1][0], "sendMessage")
            keyboard = message["reply_markup"]
            self.assertTrue(keyboard["resize_keyboard"])
            buttons = [button for row in keyboard["keyboard"] for button in row]
            expected = {key for row in listener.KEYBOARD_ROWS for key in row}
            self.assertEqual(len(buttons), 15)
            resolved = set()
            for button in buttons:
                parsed = listener.menu.parse(button["text"])
                self.assertEqual(parsed["kind"], "command")
                command = parsed["command"]
                self.assertEqual(button["text"], listener.menu.BY_KEY[command.key].label)
                self.assertIn(command.key, listener.ACTIONS | listener.READ_ONLY)
                self.assertTrue(callable(getattr(self.studio, command.key)))
                resolved.add(command.key)
            self.assertEqual(resolved, expected)

    def test_keyboard_omits_a_disabled_handler(self):
        with patch.object(listener, "ACTIONS", listener.ACTIONS - {"tech"}):
            labels = [b["text"] for row in listener.reply_keyboard()["keyboard"] for b in row]
        self.assertNotIn(listener.menu.BY_KEY["tech"].label, labels)

    def test_credentials_come_from_the_main_channel_only(self):
        with patch.dict("os.environ", {"TG_TOKEN": "", "TG_CHAT": "77"}, clear=False):
            with self.assertRaises(ValueError):
                listener.credentials()
        with patch.dict(
            "os.environ", {"TG_TOKEN": "654321:" + "y" * 35, "TG_CHAT": "77"}, clear=False
        ):
            self.assertEqual(listener.credentials()[2], "khalid_s2ss_bot")


class ClipCycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for active in (
            patch.object(listener, "HALT", self.root / "HALT"),
            patch.object(listener, "source_info", return_value=("2d0191a" + "0" * 33, 1000)),
        ):
            active.start()
            self.addCleanup(active.stop)
        self.runtime = FakeRuntime(root=self.root)
        self.started = []
        self.on_start = {}

        def start(unit):
            self.started.append(unit)
            action = self.on_start.get(unit.split("@")[0])
            if action:
                action(unit)
            return 0

        self.studio = listener.Studio(self.runtime, start, self.root / "commands/pending.json")

    def test_halt_blocks_send_but_allows_render_and_preview(self):
        self.assertEqual(self.studio.answer("أوقف"), listener.SEND_HALTED)
        self.assertTrue(listener.HALT.is_file())
        self.assertEqual(self.studio.answer("أرسل"), listener.SEND_HALTED)
        self.on_start["madar-v3-snap-render.service"] = lambda unit: self.runtime.rendered(21)
        self.assertIn("نص المعلومة", self.studio.answer("هل تعلم"))
        self.assertIn("photo", self.studio.answer("معاينة"))
        self.assertEqual(self.studio.answer("أرسل"), listener.SEND_HALTED)
        self.assertEqual(self.runtime.approved, [])
        self.assertEqual(self.started, [listener.RENDER_UNIT])

    def test_halt_revokes_confirmation_and_unhalt_never_sends(self):
        self.runtime.rendered(22)
        self.assertIn("نعم", self.studio.answer("أرسل"))
        self.studio.answer("أوقف")
        self.studio.answer("أوقف")  # idempotent
        self.assertEqual(self.studio.answer("نعم"), listener.SEND_HALTED)
        self.assertIn("تم تشغيل", self.studio.answer("شغّل"))
        self.assertFalse(listener.HALT.exists())
        self.studio.answer("شغّل")  # idempotent
        self.assertEqual(self.studio.answer("نعم"), "لا يوجد أمر ينتظر التأكيد.")
        self.assertEqual(self.runtime.approved, [])
        self.assertEqual(self.started, [])
        self.assertIn("نعم", self.studio.answer("أرسل"))

    def test_external_halt_blocks_an_already_pending_yes(self):
        self.runtime.rendered(23)
        self.studio.answer("أرسل")
        listener.HALT.touch()
        self.assertEqual(self.studio.answer("نعم"), listener.SEND_HALTED)
        self.assertEqual(self.runtime.approved, [])
        self.assertEqual(self.started, [])

    def test_a_content_command_renders_and_sends_in_one_step(self):
        sha = f"{1:064x}"

        def ack(unit):
            self.runtime.list.append(
                {"kind": "ACK", "video_sha": sha, "bot": "khalid_s2ss_bot", "message_id": 950,
                 "time": time.time()}
            )  # fmt: skip

        self.on_start["madar-v3-snap-render.service"] = lambda unit: self.runtime.rendered(1)
        self.on_start["madar-v3-snap-send-main"] = ack
        reply = self.studio.answer("هل تعلم")
        self.assertEqual(reply, "", "guard:the-clip-is-the-whole-answer")
        self.assertEqual(self.runtime.approved, [(sha, "main", True)], "guard:main-only")
        self.assertEqual(self.started, [listener.RENDER_UNIT, listener.SEND_UNIT.format(sha=sha)])
        self.assertEqual(self.studio.answer("أرسل"), listener.NO_CLIP, "guard:sent-once")

    def test_a_sent_clip_gets_no_text_reply_but_a_failure_does(self):
        calls = []

        def transport(token, method, body=None, kind=None):
            calls.append(method)
            return [update(16, "هل تعلم")] if method == "getUpdates" else {}

        with patch.object(self.studio, "answer", return_value=""):
            listener.poll_once("t", OWNER, self.studio, 0, transport)
        self.assertEqual(calls, ["getUpdates"], "guard:no-text-after-a-sent-clip")
        calls.clear()
        with patch.object(self.studio, "answer", return_value="⚠️ تعذّر"):
            listener.poll_once("t", OWNER, self.studio, 0, transport)
        self.assertEqual(calls, ["getUpdates", "sendMessage"], "guard:failures-are-reported")

    def test_a_clip_from_an_earlier_release_is_skipped_and_rendered_afresh(self):
        # The server refuses to approve it ("Evidence or release changed"); offering it
        # again made «صيام» fail on every try until it expired.
        self.runtime.rendered(25, kind="fast", title="تذكير الصيام", commit="f" * 40)
        self.assertIsNone(self.studio.pending(), "guard:stale-release-skipped")
        self.on_start["madar-v3-snap-render"] = lambda unit: self.runtime.rendered(
            26, "نص الصيام", kind="fast", title="تذكير الصيام"
        )
        self.studio.answer("صيام")
        self.assertEqual(self.started[0], "madar-v3-snap-render@fast.service")
        self.assertEqual(self.runtime.approved, [(f"{26:064x}", "main", True)])

    def test_a_waiting_clip_of_the_same_kind_is_sent_not_rendered_again(self):
        sha = self.runtime.rendered(24)
        self.studio.answer("7")
        self.assertEqual(
            self.started, [listener.SEND_UNIT.format(sha=sha)], "guard:no-wasted-render"
        )
        self.assertEqual(self.runtime.approved, [(sha, "main", True)])

    def test_variety_commands_render_their_own_kind(self):
        self.on_start["madar-v3-snap-render"] = lambda unit: self.runtime.rendered(
            11, "نصيحة تقنية", kind="tech", title="تقنية"
        )
        self.studio.answer("8")
        self.assertEqual(self.started[0], "madar-v3-snap-render@tech.service", "guard:kind-unit")
        self.assertEqual(self.started[-1], listener.SEND_UNIT.format(sha=f"{11:064x}"))
        self.on_start["madar-v3-snap-render"] = lambda unit: self.runtime.rendered(
            12, "سطر هادئ", kind="night", title="قبل النوم"
        )
        self.studio.answer("قبل النوم")
        self.assertEqual(self.started[-2], "madar-v3-snap-render@night.service")

    def test_faith_commands_render_their_sacred_kind(self):
        self.on_start["madar-v3-snap-render"] = lambda unit: self.runtime.rendered(
            13, "نص الحديث", kind="hadith", title="حديث شريف"
        )
        self.studio.answer("حديث")
        self.assertEqual(self.started[0], "madar-v3-snap-render@hadith.service", "guard:kind-unit")
        self.on_start["madar-v3-snap-render"] = lambda unit: self.runtime.rendered(
            14, "نص الدعاء", kind="dua", title="دعاء"
        )
        self.studio.answer("دعاء")
        self.assertEqual(self.started[-2], "madar-v3-snap-render@dua.service")
        self.on_start["madar-v3-snap-render"] = lambda unit: self.runtime.rendered(
            15, "نص الصيام", kind="fast", title="تذكير الصيام"
        )
        self.studio.answer("صيام")
        self.assertEqual(self.started[-2], "madar-v3-snap-render@fast.service")

    def test_preview_is_the_pending_still(self):
        self.assertEqual(self.studio.answer("معاينة"), listener.NO_CLIP)
        self.runtime.rendered(2)
        reply = self.studio.answer("👁 معاينة")
        self.assertEqual(Path(reply["photo"]).read_bytes(), b"png-bytes")
        self.assertIn("نص المعلومة", reply["caption"])

    def test_send_needs_yes_then_sends_that_clip_once(self):
        sha = self.runtime.rendered(3)
        self.assertEqual(self.studio.answer("نعم"), "لا يوجد أمر ينتظر التأكيد.")
        self.assertIn("نعم", self.studio.answer(listener.menu.BY_KEY["send"].label))
        self.assertEqual(self.started, [], "guard:keyboard-send-needs-yes")
        self.assertEqual(self.runtime.approved, [], "guard:send-needs-yes")

        def ack(unit):
            self.runtime.list.append(
                {"kind": "ACK", "video_sha": sha, "bot": "khalid_s2ss_bot", "message_id": 948,
                 "time": time.time()}
            )  # fmt: skip

        self.on_start["madar-v3-snap-send-main"] = ack
        reply = self.studio.answer("نعم")
        self.assertIn("رسالة 948", reply)
        self.assertEqual(self.runtime.approved, [(sha, "main", True)], "guard:main-only")
        self.assertEqual(self.started, [listener.SEND_UNIT.format(sha=sha)])
        self.assertEqual(self.studio.answer("نعم"), "لا يوجد أمر ينتظر التأكيد.", "guard:once")
        self.assertEqual(self.studio.answer("أرسل"), listener.NO_CLIP)

    def test_late_yes_and_changed_clip_send_nothing(self):
        self.runtime.rendered(4)
        self.studio.answer("أرسل")
        later = time.time() + listener.menu.CONFIRM_SECONDS + 1
        with patch.object(listener.time, "time", return_value=later):
            self.assertEqual(self.studio.answer("نعم"), "لا يوجد أمر ينتظر التأكيد.")
        self.studio.answer("أرسل")
        self.runtime.rendered(5)
        self.assertIn("تغيّر المقطع", self.studio.answer("نعم"), "guard:exact-sha")
        self.assertEqual(self.runtime.approved, [])
        self.assertEqual(self.started, [])

    def test_cancel_drops_the_clip_for_good(self):
        self.runtime.rendered(6)
        self.studio.answer("أرسل")
        self.assertIn("أُلغي", self.studio.answer("ألغِ"))
        self.assertEqual(self.studio.answer("نعم"), "لا يوجد أمر ينتظر التأكيد.")
        self.assertEqual(self.studio.answer("أرسل"), listener.NO_CLIP, "guard:cancel-final")

    def test_expired_clip_is_not_offered(self):
        self.runtime.rendered(7, expires=-1)
        self.assertEqual(self.studio.answer("أرسل"), listener.NO_CLIP)

    def test_failed_send_is_reported_and_locked(self):
        sha = self.runtime.rendered(8)
        self.studio.answer("أرسل")
        self.on_start["madar-v3-snap-send-main"] = lambda unit: self.runtime.list.append(
            {"kind": "UNKNOWN", "video_sha": sha, "time": time.time()}
        )
        self.assertIn("لن يُعاد", self.studio.answer("نعم"))
        self.assertEqual(self.studio.answer("أرسل"), listener.NO_CLIP)

    def test_poll_sends_the_preview_as_a_photo(self):
        self.runtime.rendered(9)
        calls = []

        def transport(token, method, body=b"", kind="application/json"):
            calls.append((method, kind))
            if method == "getUpdates":
                return [update(20, "معاينة")]
            return {"message_id": 2}

        listener.poll_once("t", OWNER, self.studio, 0, transport)
        method, kind = calls[-1]
        self.assertEqual(method, "sendPhoto")
        self.assertTrue(kind.startswith("multipart/form-data; boundary="))


if __name__ == "__main__":
    unittest.main()
