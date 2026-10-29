"""The owner's command listener for the main Snap bot (unified menu, studio_core.commands).

Long-polls Telegram for the pinned main bot and answers only the owner's private chat
(TG_CHAT), from the same person, on fresh messages. It shows the menu, the sections, the
studio state and the last video, and runs the Snap clip cycle the owner runs on the
server, through the same units:
  «هل تعلم»  renders one clip (madar-v3-snap-render) and shows its fact;
  «معاينة»   sends the pending clip's still to this chat;
  «أرسل»     asks for «نعم»; «نعم» within two minutes approves that exact SHA for the
             main channel and starts madar-v3-snap-send-main@<sha> once;
  «ألغِ»      drops the pending clip; it is never sent.
Every other command with an effect still answers that it is not in v3 yet.
Run by madar-v3-snap-commands.service: python -m snap_bot.owner_commands
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from studio_core import commands as menu

from .actions_preview import ROOT
from .agent_contract_map import BOUND, LOCAL_TOOLS
from .server_preview import STATE, Runtime, channel_bot, safe_path, source_info
from .telegram_egress import api

OFFSET = STATE / "commands" / "offset.json"
PENDING = STATE / "commands" / "pending.json"
HALT = STATE / "HALT"
SEND_HALTED = "الإرسال موقوف، اكتب «شغّل» لإعادته"
FRESH_SECONDS = 900
NOT_YET = "⏳ «{label}» لم يصل إلى v3 بعد، فلم يُنفَّذ شيء. يُضاف في إصدار قادم."
READ_ONLY = {"status", "last", "agents", "schedule", "report", "dash"}
ACTIONS = {
    "halt",
    "unhalt",
    "facts",
    "preview",
    "send",
    "cancel",
    "tech",
    "style",
    "funny",
    "night",
    "hadith",
    "dua",
    "fast",
}
RENDER_UNIT = "madar-v3-snap-render.service"
KIND_RENDER_UNIT = "madar-v3-snap-render@{kind}.service"
# Menu command key -> content kind it renders.
RENDER_KINDS = {"facts": "did_you_know", "tech": "tech", "style": "style", "funny": "funny",
                "night": "night", "hadith": "hadith", "dua": "dua", "fast": "fast"}  # fmt: skip
SEND_UNIT = "madar-v3-snap-send-main@{sha}.service"
NO_CLIP = "لا يوجد مقطع ينتظر. اكتب «هل تعلم» لصنع مقطع جديد."


# Layout contains command keys only; the shared registry owns every button label.
KEYBOARD_ROWS = (
    ("facts", "tech", "style"),
    ("funny", "night", "hadith"),
    ("dua", "fast", "preview"),
    ("send", "cancel", "status"),
    ("last",),
    ("halt", "unhalt"),
)


def reply_keyboard() -> dict[str, Any]:
    """Expose the requested v3 shortcuts only while their handlers are enabled."""
    enabled = ACTIONS | READ_ONLY
    rows = [
        [{"text": menu.BY_KEY[key].label} for key in row if key in enabled] for row in KEYBOARD_ROWS
    ]
    return {"keyboard": [row for row in rows if row], "resize_keyboard": True}


def systemctl_start(unit: str) -> int:
    """Start one oneshot unit and wait for it; the unit does the work, never this process."""
    result = subprocess.run(  # noqa: S603  # fixed argv, unit names are built here
        ["systemctl", "start", unit],  # noqa: S607  # pinned system tool
        capture_output=True,
        timeout=600,
        check=False,
    )
    return result.returncode


def owner_messages(updates: Any, owner: str, now: float) -> tuple[int, list[str]]:
    """The next offset and the owner's fresh private texts. Everything else is dropped."""
    cursor, texts = 0, []
    for update in updates if isinstance(updates, list) else []:
        uid = update.get("update_id") if isinstance(update, dict) else None
        if type(uid) is not int or uid < 0:
            continue
        cursor = max(cursor, uid + 1)
        message = update.get("message") or {}
        chat, actor = message.get("chat") or {}, message.get("from") or {}
        text, at = message.get("text"), message.get("date")
        if (
            chat.get("type") != "private"
            or str(chat.get("id")) != owner
            or str(actor.get("id")) != owner
            or actor.get("is_bot")
            or type(at) is not int
            or not 0 <= now - at <= FRESH_SECONDS
            or not isinstance(text, str)
        ):
            continue
        texts.append(text)
    return cursor, texts


def describe_event(event: dict[str, Any]) -> str:
    sha = str(event.get("video_sha", ""))[:8]
    at = time.strftime("%d/%m %H:%M", time.localtime(float(event.get("time", 0))))
    kind = event.get("kind")
    if kind == "ACK":
        return (
            "✅ أُرسل "
            + sha
            + " إلى "
            + str(event.get("bot"))
            + " · رسالة "
            + str(event.get("message_id"))
            + " · "
            + at
        )
    if kind == "HUMAN_FRAME":
        return "👍 موافقتك على " + sha + " · " + at
    if kind == "RENDERED":
        return "🎬 رندر " + sha + " ينتظر موافقتك · " + at
    return str(kind) + " " + sha + " · " + at


class Studio:
    """The answers. Read-only answers come from the release manifest and the ledger; the
    clip cycle runs through the server units and the owner-chat approval."""

    def __init__(
        self,
        runtime: Runtime | None = None,
        start: Callable[[str], int] = systemctl_start,
        pending_path: Path = PENDING,
    ) -> None:
        self.runtime = runtime or Runtime()
        self.start = start
        self.pending_path = pending_path

    def status(self) -> str:
        commit, _ = source_info()
        events = self.runtime.events()
        acks = [e for e in events if e.get("kind") == "ACK"]
        return "\n".join(
            [
                "📡 الحالة",
                "الإصدار: " + commit[:7],
                "البوت الأساسي: " + channel_bot("main"),
                "الجدولة: غير مفعّلة (كل مقطع بطلبك)",
                "المقاطع المرسلة: " + str(len(acks)),
                "آخر حدث: " + (describe_event(events[-1]) if events else "لا يوجد"),
            ]
        )

    def last(self) -> str:
        events = [e for e in self.runtime.events() if e.get("video_sha")]
        return "📥 آخر مقطع\n" + (describe_event(events[-1]) if events else "لا يوجد بعد")

    def agents(self) -> str:
        return (
            "🤖 الوكلاء في v3\n"
            + str(len(BOUND) + 1)
            + " في سلسلة كل مقطع (حتى البوابة النهائية)\n"
            + str(len(LOCAL_TOOLS))
            + " أدوات محلية: "
            + "، ".join(sorted(LOCAL_TOOLS))
        )

    def schedule(self) -> str:
        return "🗓 مواعيد الجدولة\nلا توجد جدولة مفعّلة في v3. كل مقطع يُصنع ويُرسل بطلبك."

    def report(self) -> str:
        events = self.runtime.events()
        today = time.strftime("%Y-%m-%d")
        mine = [e for e in events if time.strftime("%Y-%m-%d", time.localtime(e["time"])) == today]
        counts = {
            k: sum(1 for e in mine if e.get("kind") == k)
            for k in ("RENDERED", "HUMAN_FRAME", "ACK")
        }
        return (
            "📊 تقرير اليوم\nرندر: "
            + str(counts["RENDERED"])
            + " · موافقات: "
            + str(counts["HUMAN_FRAME"])
            + " · إرسال: "
            + str(counts["ACK"])
        )

    def dash(self) -> str:
        return "🖥️ لوحة التحكم: «غرفة قيادة مدار» في Claude (خاصة بك)."

    # The clip cycle ------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        try:
            value = json.loads(safe_path(self.pending_path).read_text())
        except FileNotFoundError:
            return {"confirm": None, "cancelled": []}
        if not isinstance(value, dict):
            raise ValueError("Invalid pending state")
        return {"confirm": value.get("confirm"), "cancelled": list(value.get("cancelled", []))}

    def save(self, state: dict[str, Any]) -> None:
        path = safe_path(self.pending_path)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(state))
        os.replace(temporary, path)

    def pending(self) -> dict[str, Any] | None:
        """The newest rendered clip that is unsent, unapproved, not cancelled and unexpired."""
        events = self.runtime.events()
        cancelled = set(self.state()["cancelled"])
        decided = {
            e.get("video_sha")
            for e in events
            if e.get("kind") in ("HUMAN_FRAME", "INTENT", "ACK", "UNKNOWN", "PRE_NETWORK_BLOCKED")
        }
        for event in reversed(events):
            sha = event.get("video_sha")
            if event.get("kind") != "RENDERED" or sha in decided or sha in cancelled:
                continue
            clip = self.clip(event)
            if clip and clip["expires_at"] > time.time():
                return clip
            return None
        return None

    def clip(self, event: dict[str, Any]) -> dict[str, Any] | None:
        preview = event.get("preview_id")
        if not isinstance(preview, str) or not re.fullmatch("[0-9a-f]{32}", preview):
            return None
        try:
            directory = safe_path(self.runtime.root / "previews" / preview)
            data = json.loads((directory / "preview.json").read_text())
            job = data["job_id"]
            if not re.fullmatch("[0-9a-f]{32}", job):
                return None
            content = json.loads(
                (directory / "state/renders" / job / "input/content.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return {
            "sha": event["video_sha"],
            "kind": content.get("kind", "did_you_know"),
            "title": content.get("title", "هل تعلم؟"),
            "body": content.get("body", ""),
            "category": content.get("category", ""),
            "expires_at": float(data["expires_at"]),
            "still": directory / "preview.png",
        }

    def describe(self, clip: dict[str, Any]) -> str:
        minutes = max(0, int((clip["expires_at"] - time.time()) / 60))
        return (
            str(clip["title"])
            + "\n"
            + str(clip["body"])
            + "\n\nالمقطع "
            + str(clip["sha"])[:8]
            + " · صالح "
            + str(minutes)
            + " دقيقة.\nاكتب «معاينة» لترى صورته، أو «أرسل» لإرساله، أو «ألغِ»."
        )

    def make(self, kind: str) -> str:
        """The owner's command is the decision: render one clip of this kind and send it.

        A pending clip of the same kind is sent instead of rendering another. «أوقف» keeps
        the clip waiting; «معاينة», «أرسل» and «ألغِ» still work on a waiting clip.
        """
        clip = self.pending()
        if not clip or clip["kind"] != kind:
            unit = RENDER_UNIT if kind == "did_you_know" else KIND_RENDER_UNIT.format(kind=kind)
            if self.start(unit) != 0:
                return "⚠️ تعذّر صنع المقطع، ولم يُرسل شيء. افحص " + unit + " على السيرفر."
            clip = self.pending()
            if not clip or clip["kind"] != kind:
                return "⚠️ انتهى الرندر بلا مقطع صالح، ولم يُرسل شيء."
        if safe_path(HALT).exists():
            return self.describe(clip) + "\n\n" + SEND_HALTED
        return str(clip["title"]) + "\n" + str(clip["body"]) + "\n\n" + self.deliver(clip["sha"])

    def facts(self) -> str:
        return self.make("did_you_know")

    def tech(self) -> str:
        return self.make("tech")

    def style(self) -> str:
        return self.make("style")

    def funny(self) -> str:
        return self.make("funny")

    def night(self) -> str:
        return self.make("night")

    def hadith(self) -> str:
        return self.make("hadith")

    def dua(self) -> str:
        return self.make("dua")

    def fast(self) -> str:
        return self.make("fast")

    def preview(self) -> Any:
        clip = self.pending()
        if not clip:
            return NO_CLIP
        return {"photo": clip["still"], "caption": self.describe(clip)}

    def halt(self) -> str:
        path = safe_path(HALT)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        state = self.state()
        state["confirm"] = None
        self.save(state)
        return SEND_HALTED

    def unhalt(self) -> str:
        # Resuming never approves a clip or revives an earlier confirmation.
        state = self.state()
        state["confirm"] = None
        self.save(state)
        safe_path(HALT).unlink(missing_ok=True)
        return "تم تشغيل الإرسال. اكتب «أرسل» ثم «نعم» لإرسال المقطع."

    def send(self) -> str:
        if safe_path(HALT).exists():
            return SEND_HALTED
        clip = self.pending()
        if not clip:
            return NO_CLIP
        state = self.state()
        state["confirm"] = {"sha": clip["sha"], "at": time.time()}
        self.save(state)
        return menu.confirm_text(menu.BY_KEY["send"]) + "\nالمقطع " + str(clip["sha"])[:8]

    def cancel(self) -> str:
        clip = self.pending()
        state = self.state()
        state["confirm"] = None
        if not clip:
            self.save(state)
            return NO_CLIP
        state["cancelled"] = (state["cancelled"] + [clip["sha"]])[-200:]
        self.save(state)
        return "❌ أُلغي المقطع " + str(clip["sha"])[:8] + ". لن يُرسل."

    def yes(self) -> str:
        state = self.state()
        confirm = state["confirm"]
        state["confirm"] = None
        self.save(state)
        if safe_path(HALT).exists():
            return SEND_HALTED
        if not isinstance(confirm, dict) or not 0 <= time.time() - confirm.get("at", 0) < (
            menu.CONFIRM_SECONDS
        ):
            return "لا يوجد أمر ينتظر التأكيد."
        clip = self.pending()
        if not clip or clip["sha"] != confirm.get("sha"):
            return "تغيّر المقطع المنتظر، فلم يُرسل شيء. اكتب «أرسل» من جديد."
        return self.deliver(clip["sha"])

    def deliver(self, sha: str) -> str:
        """Approve one clip from the owner chat and send it to the main channel, once."""
        try:
            self.runtime.approve(sha, "main", owner_chat=True)
        except Exception as error:
            return "⚠️ لم تُسجَّل الموافقة (" + type(error).__name__ + ")، فلم يُرسل شيء."
        self.start(SEND_UNIT.format(sha=sha))
        outcome = [e for e in self.runtime.events() if e.get("video_sha") == sha]
        last = outcome[-1] if outcome else {}
        if last.get("kind") == "ACK":
            return describe_event(last)
        return (
            "⚠️ لم يتأكد الإرسال (" + str(last.get("kind", "?")) + "). المقطع مقفل ولن يُعاد إرساله."
        )

    def answer(self, text: str) -> Any:
        parsed = menu.parse(text)
        kind = parsed["kind"]
        if kind == "section":
            return menu.section_text(parsed["section"])
        if kind == "command":
            command = parsed["command"]
            if command.key in READ_ONLY or command.key in ACTIONS:
                return getattr(self, command.key)()
            return NOT_YET.format(label=command.label)
        if kind == "add_fact":
            return NOT_YET.format(label=menu.BY_KEY["add_fact"].label)
        if kind == "yes":
            return self.yes()
        return menu.menu_text()


def load_offset() -> int:
    path = safe_path(OFFSET)
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return 0
    if not isinstance(value, dict) or type(value.get("offset")) is not int or value["offset"] < 0:
        raise ValueError("Invalid command offset")
    return int(value["offset"])


def save_offset(offset: int) -> None:
    path = safe_path(OFFSET)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"offset": offset}))
    os.replace(temporary, path)


def credentials() -> tuple[str, str, str]:
    token, owner = os.environ.get("TG_TOKEN", ""), os.environ.get("TG_CHAT", "")
    if not re.fullmatch(r"[0-9]{6,15}:[A-Za-z0-9_-]{30,80}", token) or not re.fullmatch(
        r"[0-9]+", owner
    ):
        raise ValueError("Invalid main credentials")
    return token, owner, channel_bot("main")


def poll_once(token: str, owner: str, studio: Studio, offset: int, transport: Any = api) -> int:
    body = json.dumps({"offset": offset, "timeout": 25, "allowed_updates": ["message"]}).encode()
    updates = transport(token, "getUpdates", body)
    cursor, texts = owner_messages(updates, owner, time.time())
    for text in texts:
        reply = studio.answer(text)
        if isinstance(reply, dict):
            body, kind = photo_body(owner, Path(reply["photo"]).read_bytes(), reply["caption"])
            transport(token, "sendPhoto", body, kind)
            continue
        message = {"chat_id": owner, "text": reply}
        if menu.parse(text)["kind"] == "menu":
            message["reply_markup"] = reply_keyboard()
        transport(token, "sendMessage", json.dumps(message).encode())
    return max(offset, cursor)


def photo_body(chat: str, photo: bytes, caption: str) -> tuple[bytes, str]:
    boundary = "madar" + uuid.uuid4().hex
    while boundary.encode() in photo:
        boundary = "madar" + uuid.uuid4().hex
    parts = []
    for name, value in (("chat_id", chat), ("caption", caption[:1024])):
        parts.append(
            (
                "--" + boundary + '\r\nContent-Disposition: form-data; name="' + name + '"'
                "\r\n\r\n" + value + "\r\n"
            ).encode()
        )
    parts.append(
        (
            "--" + boundary + '\r\nContent-Disposition: form-data; name="photo"; '
            'filename="preview.png"\r\nContent-Type: image/png\r\n\r\n'
        ).encode()
        + photo
        + ("\r\n--" + boundary + "--\r\n").encode()
    )
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def main() -> None:
    try:
        if os.geteuid() != 0:
            raise ValueError("Root controller required")
        token, owner, bot = credentials()
        if api(token, "getMe").get("username") != bot:
            raise ValueError("Wrong main bot")
        studio = Studio()
        offset = load_offset()
        print(
            json.dumps({"event": "owner_commands.ready", "bot": bot, "root": str(ROOT)}), flush=True
        )
        while True:
            next_offset = poll_once(token, owner, studio, offset)
            if next_offset != offset:
                save_offset(next_offset)
                offset = next_offset
    except Exception as error:
        # Fixed strings only: never the token, a URL or a message body.
        print("OWNER_COMMANDS_BLOCKED: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
