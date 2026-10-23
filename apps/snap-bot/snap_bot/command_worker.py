"""Persistent owner-command worker; AF_UNIX only and no Telegram token."""

import argparse
import hashlib
import json
import os
import socket
import time
import tomllib
from pathlib import Path
from typing import Any

from studio_core.config import Config
from studio_core.jobs import GatewayLimit, Repository
from studio_core.jobs.gateway import CommandGateway
from studio_core.repair.adoption import expire_pending_adoptions
from studio_core.storage import Storage

from .command_transport import Client
from .service import execute
from .telegram_egress import (
    DeliveryConfirmed,
    deliver,
    reconcile_evidence_sweep,
    reconcile_published,
)


def initialize(repo: Repository) -> None:
    with repo.transaction() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS telegram_cursor(id INTEGER PRIMARY KEY CHECK(id=1),offset INTEGER NOT NULL)"
        )
        db.execute("INSERT OR IGNORE INTO telegram_cursor VALUES(1,0)")


def accept_batch(repo: Repository, gateway: CommandGateway, batch: Any, owner: str) -> list[Any]:
    accepted: list[Any] = []
    for command in batch["commands"]:
        uid = command["update_id"]
        kind = command["command"]
        if command["actor"] != owner:
            raise ValueError("Wrong owner")
        if kind == "weather":
            body = {
                "channel": "telegram",
                "actor": owner,
                "event_id": str(uid),
                "command": "weather",
                "priority": "NORMAL",
            }
            now = time.time()
            try:
                job, created = gateway.accept(body, now, gateway.signature(body, now))
                accepted.append(
                    {
                        "update_id": uid,
                        "command": kind,
                        "job_id": job["id"],
                        "created": created,
                        "reply": "queued" if created else None,
                    }
                )
            except GatewayLimit:
                accepted.append({"update_id": uid, "command": kind, "reply": "busy"})
        else:
            accepted.append(
                {
                    "update_id": uid,
                    "command": kind,
                    "reply": "status" if kind == "status" else "help",
                }
            )
    # Gateway event IDs are stable: crash before cursor commit cannot create another job.
    with repo.transaction() as db:
        db.execute("UPDATE telegram_cursor SET offset=MAX(offset,?) WHERE id=1", (batch["offset"],))
    return accepted


def baseline(config: Config, settings: dict[str, Any]) -> str:
    source = Path(__file__).with_name("render.py")
    value = {
        "renderer": hashlib.sha256(source.read_bytes()).hexdigest(),
        "profile": settings["profile"],
        "city": settings["city"],
    }
    digest = hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return digest


def network_guard() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, family, kind in [
        ("ipv4", socket.AF_INET, socket.SOCK_STREAM),
        ("ipv6", socket.AF_INET6, socket.SOCK_STREAM),
        ("udp", socket.AF_INET, socket.SOCK_DGRAM),
    ]:
        try:
            with socket.socket(family, kind):
                raise RuntimeError("Worker network isolation missing")
        except OSError as exc:
            if exc.errno not in (1, 13, 97):
                raise
            result[name] = "DENIED"
    return result


def notify(value: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if address:
        if address.startswith("@"):
            address = "\0" + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2)
            sock.connect(address)
            sock.sendall(value.encode())


def run(config: Config, settings: dict[str, Any], approved: str, client: Any) -> None:
    owner = os.environ["TG_TEST_CHAT"]
    secret = os.environ["STUDIO_GATEWAY_SECRET"]
    if config.owners.get("telegram") != (owner,):
        raise ValueError("Unpinned Telegram owner")
    if baseline(config, settings) != approved:
        raise ValueError("Reviewed renderer baseline changed")
    if os.environ.get("TG_TEST_TOKEN"):
        raise ValueError("Worker must not receive Telegram token")
    isolation = network_guard()
    repo = Repository(config)
    gateway = CommandGateway(config, repo, secret)
    initialize(repo)
    last_gc: float = 0
    last_evidence: float = 0
    evidence_after = ""
    # Startup repair precedes GC and never contacts Telegram.
    evidence = safe_evidence_sweep(config)
    evidence_after = evidence["after"]
    last_evidence = time.time()
    print(json.dumps({"event": "delivery.evidence.sweep", **evidence}), flush=True)
    client.call("/reply", {"key": "listener-v1-ready", "message": "ready"})
    print(
        json.dumps(
            {
                "event": "telegram.worker.ready",
                "baseline": approved,
                "network_guard": isolation,
                "telegram_token_present": False,
            }
        ),
        flush=True,
    )
    notify("READY=1\nWATCHDOG=1")

    def delivery(cfg: Config, job_id: str, digest: str, lease: str) -> Any:
        if baseline(cfg, settings) != approved:
            raise ValueError("Baseline changed")
        return deliver(
            cfg,
            job_id,
            digest,
            client.telegram,
            lease_token=lease,
            review_mode="PINNED_WEATHER_TEMPLATE",
        )

    while True:
        try:
            expire_pending_adoptions(repo)
            claim = repo.claim()
            if claim:
                try:
                    result = execute(
                        config,
                        settings,
                        secret,
                        claim=claim,
                        delivery=delivery,
                        watchdog=lambda: notify("WATCHDOG=1"),
                    )
                    print(
                        json.dumps(
                            {
                                "event": "command.completed",
                                "job_id": claim["id"],
                                "state": result["state"],
                                "receipt": result.get("delivery_receipt"),
                            }
                        ),
                        flush=True,
                    )
                    if result["state"] != "COMPLETED":
                        try:
                            client.call(
                                "/reply", {"key": claim["id"] + ":failed", "message": "failed"}
                            )
                        except Exception:
                            pass
                    elif result.get("delivery_receipt", {}).get("replayed") is False:
                        try:
                            client.call(
                                "/reply",
                                {"key": claim["id"] + ":unchanged", "message": "unchanged"},
                            )
                        except Exception:
                            pass
                except DeliveryConfirmed as exc:
                    confirmed_failure(config, claim["id"], exc)
                except Exception as exc:
                    print(
                        json.dumps(
                            {
                                "event": "command.failed",
                                "job_id": claim["id"],
                                "error": type(exc).__name__,
                            }
                        ),
                        flush=True,
                    )
                    try:
                        client.call("/reply", {"key": claim["id"] + ":failed", "message": "failed"})
                    except Exception:
                        pass
            with repo.connect() as db:
                offset = db.execute("SELECT offset FROM telegram_cursor WHERE id=1").fetchone()[0]
            batch = client.call("/poll", {"offset": offset})
            for command in accept_batch(repo, gateway, batch, owner):
                print(
                    json.dumps(
                        {
                            "event": "command.accepted",
                            "update_id": command["update_id"],
                            "command": command["command"],
                            "job_id": command.get("job_id"),
                            "created": command.get("created"),
                        }
                    ),
                    flush=True,
                )
                if command.get("reply"):
                    try:
                        client.call(
                            "/reply",
                            {
                                "key": str(command["update_id"]) + ":reply",
                                "message": command["reply"],
                            },
                        )
                    except Exception:
                        pass
            if time.time() - last_evidence >= 60:
                evidence = safe_evidence_sweep(config, after=evidence_after)
                evidence_after = evidence["after"]
                last_evidence = time.time()
                print(json.dumps({"event": "delivery.evidence.sweep", **evidence}), flush=True)
            if time.time() - last_gc > 3600:
                Storage(config).gc(repo)
                last_gc = time.time()
            path = config.data_root / "listener-health.json"
            temp = path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(
                    {"heartbeat": time.time(), "offset": batch["offset"], "baseline": approved}
                )
            )
            temp.replace(path)
            notify("WATCHDOG=1")
        except Exception as exc:
            print(json.dumps({"event": "listener.retry", "error": type(exc).__name__}), flush=True)
            time.sleep(5)


def safe_evidence_sweep(config: Config, after: str = "") -> dict[str, Any]:
    try:
        return reconcile_evidence_sweep(config, after=after)
    except Exception:
        # Receipt maintenance must not prevent readiness or the listener heartbeat.
        print(json.dumps({"event": "delivery.evidence.sweep.failed"}), flush=True)
        return {"after": after, "repaired": 0, "pending": 0, "skipped": 0, "failed": True}


def confirmed_failure(config: Config, job_id: str, error: Any) -> Any:
    if error.job_id != job_id:
        raise ValueError("Receipt job mismatch")
    try:
        receipt = reconcile_published(config, job_id, error.video_sha, error.review_mode)
        print(
            json.dumps(
                {
                    "event": "command.completed",
                    "job_id": job_id,
                    "state": "COMPLETED",
                    "receipt": receipt,
                }
            ),
            flush=True,
        )
        return receipt
    except Exception:
        # The video is confirmed: no failed reply and no send retry.
        print(
            json.dumps({"event": "command.delivery_confirmed_evidence_pending", "job_id": job_id}),
            flush=True,
        )
        return None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--socket", type=Path, required=True)
    p.add_argument("--baseline", required=True)
    a = p.parse_args()
    config = Config.load(a.config)
    settings = tomllib.loads(a.config.read_text())["slice"]
    try:
        run(config, settings, a.baseline, Client(a.socket))
    except Exception as exc:
        print(json.dumps({"event": "worker.stopped", "error": type(exc).__name__}), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
