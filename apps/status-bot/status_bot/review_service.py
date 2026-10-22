"""Explicit listener assembly; no worker, retries, Graph token or installation."""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from studio_core.config import Config
from studio_core.jobs import Repository

from .owner_review import OwnerConfig, OwnerReview
from .review_http import Endpoint, PrivateServer, WebhookConfig
from .whatsapp_contract import DeliveryIdentity, parse_json, require
from .whatsapp_outbox import WhatsAppOutbox, read_bound


def assemble(
    mode: str,
    config_path: str | os.PathLike[str],
    settings_path: str | os.PathLike[str],
    environment: Mapping[str, str],
) -> Endpoint:
    """Called only after explicit activation. Environment is never printed."""

    def secret(name: str) -> Any:
        value = environment.get(name)
        require(type(value) is str and 0 < len(value) <= 4096, "REVIEW_ENV_MISSING")
        return value

    config = Config.load(Path(config_path))
    require(
        config.app == "status-bot-v2" and config.command_allowlist == ("weather",),
        "REVIEW_CONFIG_SCOPE_INVALID",
    )
    settings_path = Path(settings_path).absolute()
    settings = parse_json(read_bound(settings_path.parent, settings_path.name, 8192))
    expected = {"origin", "owner"} if mode == "owner" else {"host"}
    require(type(settings) is dict and set(settings) == expected, "REVIEW_SETTINGS_INVALID")
    identity = DeliveryIdentity(
        secret("STATUS_WA_BUSINESS_ACCOUNT"),
        secret("STATUS_WA_PHONE_NUMBER"),
        secret("STATUS_WA_RECIPIENT"),
        secret("STATUS_WA_GATE_KEY").encode(),
        secret("STATUS_WA_WEBHOOK_KEY").encode(),
    )
    if mode == "owner":
        owner_config = OwnerConfig(
            settings["origin"],
            settings["owner"],
            secret("STATUS_OWNER_LOGIN_SECRET").encode(),
            secret("STATUS_OWNER_SESSION_KEY").encode(),
        )
        require(
            len(
                {
                    owner_config.login_secret,
                    owner_config.session_key,
                    identity.gate_key,
                    identity.webhook_key,
                }
            )
            == 4,
            "REVIEW_KEYS_INVALID",
        )
    else:
        require(mode == "webhook", "HTTP_MODE_INVALID")
        webhook_config = WebhookConfig(settings["host"], secret("STATUS_WA_VERIFY_TOKEN").encode())
        require(
            webhook_config.verify_token not in (identity.gate_key, identity.webhook_key),
            "WEBHOOK_KEYS_INVALID",
        )
    # No DB mutation until all settings/keys have been validated.
    outbox = WhatsAppOutbox(Repository(config), identity)
    if mode == "owner":
        return Endpoint("owner", OwnerReview(outbox, owner_config))
    return Endpoint("webhook", outbox, webhook_config)


def main(argv: Sequence[str] | None = None, environment: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Private Status owner review or separate WhatsApp receipts listener"
    )
    parser.add_argument("--mode", required=True, choices=("owner", "webhook"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--proxy-uid", required=True, type=int)
    parser.add_argument("--enable-listener", action="store_true")
    args = parser.parse_args(argv)
    server: PrivateServer | None = None
    try:
        require(args.enable_listener, "HTTP_SERVICE_DISABLED")
        endpoint = assemble(
            args.mode,
            args.config,
            args.settings,
            os.environ if environment is None else environment,
        )
        server = PrivateServer(args.socket, endpoint, args.proxy_uid, enabled=True)
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("REVIEW_SERVICE_BLOCKED", file=sys.stderr)
        return 2
    finally:
        if server is not None:
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
