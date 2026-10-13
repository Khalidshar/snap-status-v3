import hashlib
import hmac
import math
import re
import time
from collections.abc import Callable
from typing import Any

from studio_core.config import Config
from studio_core.jobs import PRIORITIES, canonical
from studio_core.jobs.repository import Repository


class Unauthorized(PermissionError):
    pass


class CommandGateway:
    def __init__(
        self,
        config: Config,
        repository: Repository,
        secret: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("Gateway signing secret too short")
        self.config, self.repository, self.secret, self.clock = config, repository, secret, clock

    def signature(self, body: Any, timestamp: float) -> str:
        return hmac.new(
            self.secret.encode(), (str(timestamp) + "." + canonical(body)).encode(), hashlib.sha256
        ).hexdigest()

    def accept(self, body: Any, timestamp: float, signature: str) -> tuple[dict[str, Any], bool]:
        now = self.clock()
        if (
            type(timestamp) not in (int, float)
            or not math.isfinite(timestamp)
            or abs(now - timestamp) > self.config.signature_skew_seconds
        ):
            raise Unauthorized("Invalid timestamp")
        if not isinstance(body, dict) or len(canonical(body).encode()) > 8192:
            raise ValueError("Invalid command body")
        if not isinstance(signature, str) or not hmac.compare_digest(
            self.signature(body, timestamp), signature
        ):
            raise Unauthorized("Invalid signature")
        if set(body) - {"channel", "actor", "event_id", "command", "priority", "expires_at"}:
            raise ValueError("Unknown request field")
        channel, actor = body.get("channel"), body.get("actor")
        if (
            not isinstance(channel, str)
            or not isinstance(actor, str)
            or actor not in self.config.owners.get(channel, ())
        ):
            raise Unauthorized("Actor not allowed")
        command, priority, event_id = (
            body.get("command"),
            body.get("priority", "NORMAL"),
            body.get("event_id"),
        )
        if (
            not isinstance(command, str)
            or not isinstance(priority, str)
            or command not in self.config.command_allowlist
            or priority not in PRIORITIES
        ):
            raise ValueError("Unsupported command or priority")
        if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event_id):
            raise ValueError("Invalid event identifier")
        deadline = now + self.config.budgets[priority]
        if "expires_at" in body:
            value = body["expires_at"]
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("Invalid expiry")
            deadline = min(deadline, value)
        actor_hash = hmac.new(
            self.secret.encode(), (channel + ":" + actor).encode(), hashlib.sha256
        ).hexdigest()
        payload_hash = hashlib.sha256(canonical(body).encode()).hexdigest()
        return self.repository.enqueue(
            channel + ":" + event_id, actor_hash, command, priority, payload_hash, deadline
        )
