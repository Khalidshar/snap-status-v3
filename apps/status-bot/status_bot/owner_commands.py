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
from studio_core.sources.live import LiveNews
from studio_core.sources.news import TITLES, NewsUnavailable

from .command_inbox import claim
from .server_preview import GRAPH_VERSION, Runtime
from .whatsapp_contract import DeliveryIdentity, require, response_id
from .whatsapp_egress import GraphCredentials, GraphTransport
from .whatsapp_outbox import WhatsAppOutbox


def answer(text: str, runtime: Any) -> str:
    parsed = menu.parse(text)
    if text.startswith("s:") and text[2:] in {row[0] for row in menu.SECTIONS}:
        return menu.section_text(text[2:])
    if parsed["kind"] == "section":
        return menu.section_text(parsed["section"])
    if parsed["kind"] != "command":
        return menu.menu_text()
    key = parsed["command"].key
    if key == "schedule":
        return "الجدولة غير مفعلة؛ كل طلب يصلك بناءً على أمرك."
    if key in {"status", "report", "last"}:
        events = runtime.events()
        accepted = sum(e.get("kind") == "ACK" for e in events)
        return (
            "بوت الحالات يعمل على واتساب.\nالمقاطع التي قبلتها المنصة: "
            + str(accepted)
            + "\nالنشر كحالة يتم يدويًا."
        )
    return (
        "«"
        + str(parsed["command"].label)
        + "» لا يملك مسار أوامر مكتملًا في الحالات بعد؛ لم يُنفّذ شيء."
    )


def process_one(outbox: Any, transport: Any, news: Any, runtime: Any) -> bool:
    item = claim(outbox.repo, outbox.identity)
    if item is None:
        return False

    def send(text: str) -> None:
        with outbox.repo.transaction() as db:
            db.execute(
                "UPDATE whatsapp_commands SET state='SEND_INTENT' WHERE tag=?", (item["tag"],)
            )
        response_id(transport.send_text(text), "send", outbox.identity.recipient)

    try:
        parsed = menu.parse(item["text"])
        key = parsed["command"].key if parsed["kind"] == "command" else None
        if key in TITLES:
            try:
                sent = news.reply(key, item["tag"], send)
                if not sent:
                    raise ValueError("Command already consumed")
            except NewsUnavailable:
                send("لا يوجد خبر عربي حديث مناسب الآن؛ لم يُستبدل بمحتوى قديم.")
        else:
            send(answer(item["text"], runtime))
        with outbox.repo.transaction() as db:
            db.execute("UPDATE whatsapp_commands SET state='ACK' WHERE tag=?", (item["tag"],))
    except Exception:
        # An interrupted POST remains consumed. Never replay an uncertain reply.
        with outbox.repo.transaction() as db:
            db.execute(
                "UPDATE whatsapp_commands SET state='BLOCKED_OR_UNKNOWN',code='COMMAND_FAILED' WHERE tag=?",
                (item["tag"],),
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
            if not process_one(outbox, transport, news, runtime):
                time.sleep(1)
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("STATUS_OWNER_COMMANDS_BLOCKED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
