"""Owner-only WhatsApp command consumer. No retries, video approval or scheduling.

Consumes messages committed by WhatsAppOutbox.webhook after signature validation.
Written news is copied with attribution through Network Core. Existing video
delivery and human review contracts remain independent.
"""

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

from studio_core import commands as menu
from studio_core.config import Config
from studio_core.jobs import Repository
from studio_core.observability.failure_code import (
    NO_FRESH_CONTENT_MESSAGE,
    NO_FRESH_FASHION_MESSAGE,
    NoFreshContent,
)
from studio_core.production.fashion_policy import KEYS as FASHION_KEYS
from studio_core.production.fashion_policy import FashionUnavailable
from studio_core.sources.live import LiveNews
from studio_core.sources.news import TITLES

from . import content_clips, tool_commands
from .command_control import STOPPED, change, halted, status_text
from .command_inbox import claim
from .server_preview import GRAPH_VERSION, STATE, Runtime
from .service_window import ensure as ensure_window
from .service_window import text as window_text
from .whatsapp_contract import DeliveryBlocked, DeliveryIdentity, require, response_id
from .whatsapp_egress import GraphCredentials, GraphTransport
from .whatsapp_outbox import WhatsAppOutbox


def answer(text: str, runtime: Any, *, outbox: Any = None) -> str:
    parsed = menu.parse(text)
    if text.startswith("s:") and text[2:] in {row[0] for row in menu.SECTIONS}:
        return menu.section_text(text[2:])
    if parsed["kind"] == "section":
        return menu.section_text(parsed["section"])
    if parsed["kind"] == "add_fact":
        # Owner 21 Nov 2026: the fact joins a review queue; nothing is published.
        from studio_core.observability.agent_tools import add_fact

        return add_fact(STATE, str(parsed["text"]))
    if parsed["kind"] != "command":
        return menu.menu_text()
    key = parsed["command"].key
    if key == "add_fact":
        from studio_core.observability.agent_tools import FACT_HELP

        return FACT_HELP
    if key in {"preview", "send", "cancel"}:
        # WhatsApp has no waiting clip: each clip reaches the owner as soon as it passes
        # the 18 agents and the auditor (owner decision); the approval cycle is Snap's.
        return (
            "في واتساب ما فيه مقطع ينتظر موافقة: كل مقطع يوصلك مباشرة بعد ما يمر على"
            " الوكلاء الـ١٨ والمدقق.\n«معاينة» و«أرسل» و«ألغِ» تخص بوت سناب."
        )
    if key == "dash":
        return "🖥️ لوحة التحكم: «غرفة قيادة مدار» في Claude (خاصة بك)."
    if key in {"schedule", "pause", "resume"}:
        from datetime import datetime

        from studio_core.jobs import schedule

        if key == "pause":
            schedule.set_paused(STATE, True)
            return "⏸ أوقفت جدولة واتساب. أوامرك اليدوية شغالة. اكتب «تشغيل الجدولة» لترجع."
        if key == "resume":
            schedule.set_paused(STATE, False)
            return "▶️ رجعت جدولة واتساب. اكتب «مواعيد الجدولة» تشوف مواعيد اليوم."
        return schedule.schedule_text("status", datetime.now(schedule.RIYADH), STATE)
    if key in {"status", "report", "last"}:
        events = runtime.events()
        accepted = sum(e.get("kind") == "ACK" for e in events)
        return (
            "بوت الحالات يعمل على واتساب.\nالمقاطع التي قبلتها المنصة: "
            + str(accepted)
            + "\nالنشر كحالة يتم يدويًا."
            + "\n"
            + status_text()
            + ("\n" + window_text(outbox) if outbox is not None else "")
        )
    return (
        "«"
        + str(parsed["command"].label)
        + "» لا يملك مسار أوامر مكتملًا في الحالات بعد؛ لم يُنفّذ شيء."
    )


def menu_list(text: str) -> tuple[str, dict[str, Any]] | None:
    """The interactive list for a menu or section request; None for anything else."""
    parsed = menu.parse(text)
    section = None
    if text.startswith("s:") and text[2:] in {row[0] for row in menu.SECTIONS}:
        section = text[2:]
    elif parsed["kind"] == "section":
        section = parsed["section"]
    elif parsed["kind"] != "menu":
        return None
    if section is None:
        return "🤖 اضغط «القائمة» واختر القسم.", menu.whatsapp_list()
    title = next(t for s, t, _ in menu.SECTIONS if s == section)
    return title + "\nاضغط «القائمة» واختر الأمر.", menu.whatsapp_list(section)


def process_one(outbox: Any, transport: Any, news: Any, runtime: Any) -> bool:
    from studio_core.jobs.panel_queue import transfer_status

    transfer_status(STATE / "panel-commands.sqlite", outbox)
    item = claim(outbox.repo, outbox.identity)
    if item is None:
        return False

    def send(text: str) -> None:
        ensure_window(outbox)
        with outbox.repo.transaction() as db:
            db.execute(
                "UPDATE whatsapp_commands SET state='SEND_INTENT' WHERE tag=?", (item["tag"],)
            )
        response_id(transport.send_text(text), "send", outbox.identity.recipient)

    try:
        ensure_window(outbox)
        parsed = menu.parse(item["text"])
        key = parsed["command"].key if parsed["kind"] == "command" else None
        if key in {"halt", "unhalt"}:
            send(change(key == "halt", runtime, item["tag"]))
        elif (
            parsed["kind"] == "command"
            and (
                parsed["command"].section in {"news", "general", "fashion", "faith", "brief"}
                or key == "send"
            )
            and halted()
        ):
            send(STOPPED)
        elif production_content(key):
            from studio_core.observability.failure_code import WORKING_MESSAGE

            # A courtesy notice outside the command's single recorded reply; never blocks.
            try:
                transport.send_text(WORKING_MESSAGE.format(label=parsed["command"].label))
            except Exception:  # the clip itself is the answer
                print("STATUS_WORKING_NOTICE_SKIPPED", file=sys.stderr)
            try:
                content_clips.send(outbox, transport, item, str(key), runtime)
            except NoFreshContent:
                if key != "urgent":
                    raise
                send(NO_FRESH_CONTENT_MESSAGE)
            except FashionUnavailable:
                if key not in FASHION_KEYS:
                    raise
                send(NO_FRESH_FASHION_MESSAGE)
        elif key == "brief" or key in TITLES:
            from studio_core.observability.failure_code import LEGACY_LOCKED_MESSAGE

            # Pre-production paths, locked (owner 21 Nov 2026): they send without the 18 agents.
            print("STATUS_LEGACY_PATH_LOCKED", file=sys.stderr)
            send(LEGACY_LOCKED_MESSAGE.format(label=parsed["command"].label))
        elif key == "agents":
            send(tool_commands.status(outbox))
        elif key in tool_commands.NAMES:
            send(tool_commands.execute(key, outbox))
        elif key in content_clips.KINDS:
            content_clips.send(outbox, transport, item, key, runtime)
        else:
            choices = menu_list(item["text"])
            if choices is not None and hasattr(transport, "send_list"):
                # Same tappable choices as the Telegram keyboard instead of "type a number".
                ensure_window(outbox)
                with outbox.repo.transaction() as db:
                    db.execute(
                        "UPDATE whatsapp_commands SET state='SEND_INTENT' WHERE tag=?",
                        (item["tag"],),
                    )
                response_id(transport.send_list(*choices), "send", outbox.identity.recipient)
            else:
                send(answer(item["text"], runtime, outbox=outbox))
        with outbox.repo.transaction() as db:
            db.execute("UPDATE whatsapp_commands SET state='ACK' WHERE tag=?", (item["tag"],))
    except Exception as error:
        code = (
            "WA_SERVICE_WINDOW_CLOSED"
            if isinstance(error, DeliveryBlocked) and str(error) == "WA_SERVICE_WINDOW_CLOSED"
            else "COMMAND_FAILED"
        )
        print("STATUS_COMMAND_BLOCKED:" + code, file=sys.stderr)
        # An interrupted POST remains consumed. Never replay an uncertain reply.
        with outbox.repo.transaction() as db:
            db.execute(
                "UPDATE whatsapp_commands SET state='BLOCKED_OR_UNKNOWN',code=? WHERE tag=?",
                (code, item["tag"]),
            )
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--news-config", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = Config.load(args.config)
        require(config.app == "status-bot-v2", "WA_APP_SCOPE_INVALID")
        identity = DeliveryIdentity(
            os.environ["STATUS_WA_BUSINESS_ACCOUNT"],
            os.environ["STATUS_WA_PHONE_NUMBER"],
            os.environ["STATUS_WA_RECIPIENT"],
            os.environ["STATUS_WA_GATE_KEY"].encode(),
            os.environ["STATUS_WA_WEBHOOK_KEY"].encode(),
        )
        outbox = WhatsAppOutbox(Repository(config), identity)
        transport = GraphTransport(
            GraphCredentials(
                GRAPH_VERSION,
                identity.phone_number,
                identity.recipient,
                os.environ["WA_TOKEN"],
            ),
            enabled=True,
        )
        news = LiveNews("status-bot", args.news_config, config.data_root / "news-history.sqlite3")
        print("STATUS_OWNER_COMMANDS_READY", flush=True)
        runtime = Runtime()
        while True:
            from .panel_actions import process as process_actions

            acted = process_actions(outbox, transport, runtime, STATE / "panel-commands.sqlite")
            if not process_one(outbox, transport, news, runtime) and not acted:
                time.sleep(1)
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("STATUS_OWNER_COMMANDS_BLOCKED", file=sys.stderr)
        return 2


def production_content(key: Any) -> bool:
    from studio_core.production import enabled
    from studio_core.production.model import TITLES

    return isinstance(key, str) and enabled(key) and key in TITLES


if __name__ == "__main__":
    raise SystemExit(main())
