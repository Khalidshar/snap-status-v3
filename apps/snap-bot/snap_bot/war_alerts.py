"""Snap-only alert entrypoint. No video rendering or scheduling other content."""

import argparse
import json
import tomllib
from pathlib import Path

from studio_core.sources.war_alerts import tick

from .owner_commands import HALT, credentials, live_news
from .server_preview import safe_path
from .telegram_egress import api


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args()
    policy = tomllib.loads(args.policy.read_text())
    if policy.get("enabled") is not True:
        return
    token, owner, bot = credentials()
    if api(token, "getMe").get("username") != bot:
        raise ValueError("BOT_MISMATCH")
    chat = api(token, "getChat", json.dumps({"chat_id": owner}).encode())
    if str(chat.get("id")) != owner or chat.get("type") != "private":
        raise ValueError("OWNER_MISMATCH")
    tick(
        live_news(),
        frozenset(policy["approved_sources"]),
        lambda: not safe_path(HALT).exists(),
        lambda text: api(
            token, "sendMessage", json.dumps({"chat_id": owner, "text": text}).encode()
        ),
        enabled=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("WAR_ALERT_BLOCKED") from None
