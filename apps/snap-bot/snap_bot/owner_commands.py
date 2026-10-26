"""The owner's command listener for the main Snap bot (unified menu, studio_core.commands).

Long-polls Telegram for the pinned main bot and answers only the owner's private chat
(TG_CHAT), from the same person, on fresh messages. This first stage is read-only: it
shows the menu, the sections, the studio state and the last video. Every command that
would make, send or stop something answers that it is not available in v3 yet and does
nothing; those land one by one in later releases. It never renders, approves or sends.
Run by madar-v3-snap-commands.service: python -m snap_bot.owner_commands
"""

import json
import os
import re
import sys
import time
from typing import Any

from studio_core import commands as menu

from .actions_preview import ROOT
from .agent_contract_map import BOUND, LOCAL_TOOLS
from .server_preview import STATE, Runtime, channel_bot, safe_path, source_info
from .telegram_egress import api

OFFSET = STATE / "commands" / "offset.json"
FRESH_SECONDS = 900
NOT_YET = "⏳ «{label}» لم يصل إلى v3 بعد، فلم يُنفَّذ شيء. يُضاف في إصدار قادم."
# Read-only commands this stage answers; the rest reply NOT_YET.
READ_ONLY = {"status", "last", "agents", "schedule", "report", "dash"}


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
    """The answers. Reads the release manifest and the ledger; writes nothing."""

    def __init__(self, runtime: Runtime | None = None) -> None:
        self.runtime = runtime or Runtime()

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

    def answer(self, text: str) -> str:
        parsed = menu.parse(text)
        kind = parsed["kind"]
        if kind == "section":
            return menu.section_text(parsed["section"])
        if kind == "command":
            command = parsed["command"]
            if command.key in READ_ONLY:
                return str(getattr(self, command.key)())
            return NOT_YET.format(label=command.label)
        if kind == "add_fact":
            return NOT_YET.format(label=menu.BY_KEY["add_fact"].label)
        if kind == "yes":
            return "لا يوجد أمر ينتظر التأكيد."
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
        transport(token, "sendMessage", json.dumps({"chat_id": owner, "text": reply}).encode())
    return max(offset, cursor)


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
