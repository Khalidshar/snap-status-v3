"""WhatsApp-only alert entrypoint; full stop and owner window guard every send."""

import argparse
import os
import tomllib
from pathlib import Path

from studio_core.config import Config
from studio_core.jobs import Repository
from studio_core.sources.live import LiveNews
from studio_core.sources.war_alerts import tick

from .command_control import halted
from .server_preview import GRAPH_VERSION
from .service_window import status
from .whatsapp_contract import DeliveryIdentity, response_id
from .whatsapp_egress import GraphCredentials, GraphTransport
from .whatsapp_outbox import WhatsAppOutbox


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--news-config", type=Path, required=True)
    args = parser.parse_args()
    policy = tomllib.loads(args.policy.read_text())
    if policy.get("enabled") is not True:
        return
    config = Config.load(args.config)
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
            GRAPH_VERSION, identity.phone_number, identity.recipient, os.environ["WA_TOKEN"]
        ),
        enabled=True,
    )
    news = LiveNews("status-bot", args.news_config, config.data_root / "news-history.sqlite3")

    def allowed() -> bool:
        return not halted() and status(outbox.repo, identity.binding)["open"] is True

    def send(text: str) -> None:
        if not allowed():
            raise ValueError("ALERT_BLOCKED")
        response_id(transport.send_text(text), "send", identity.recipient)

    tick(news, frozenset(policy["approved_sources"]), allowed, send, enabled=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("WAR_ALERT_BLOCKED") from None
