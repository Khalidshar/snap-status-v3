"""Enqueue one pinned weather request; delivery remains in the isolated worker."""

import argparse
import json
import os
import time
from pathlib import Path

from studio_core.config import Config
from studio_core.jobs import Repository
from studio_core.jobs.gateway import CommandGateway

from .command_worker import network_guard


def enqueue(config, secret, owner, event_id, due, now=None):
    now = time.time() if now is None else now
    if now < due:
        raise ValueError("Schedule is not due")
    if now >= due + 600:
        return {"state": "MISSED", "external_delivery": False}
    if config.owners.get("telegram") != (owner,):
        raise ValueError("Unpinned owner")
    repo = Repository(config)
    gateway = CommandGateway(config, repo, secret, clock=lambda: now)
    body = {
        "channel": "telegram",
        "actor": owner,
        "event_id": event_id,
        "command": "weather",
        "priority": "NORMAL",
        "expires_at": due + 600,
    }
    job, created = gateway.accept(body, now, gateway.signature(body, now))
    return {"job_id": job["id"], "created": created, "state": job["state"]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--event", required=True)
    p.add_argument("--due", type=int, required=True)
    a = p.parse_args()
    if os.environ.get("TG_TEST_TOKEN"):
        raise ValueError("Scheduler must not receive Telegram token")
    network_guard()
    print(
        json.dumps(
            enqueue(
                Config.load(a.config),
                os.environ["STUDIO_GATEWAY_SECRET"],
                os.environ["TG_TEST_CHAT"],
                a.event,
                a.due,
            )
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
