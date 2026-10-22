"""Secret-free delivery diagnostics. Never authorize, reconcile or retry a send."""

import json
import re
import socket
import sqlite3
import ssl
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from .telegram_errors import safe_classification

STAGES = frozenset(
    {
        "APPROVAL",
        "DURABLE_INTENT",
        "BUNDLE_VALIDATION",
        "CREDENTIAL_VALIDATION",
        "DELIVERY_VALIDATION",
        "TELEGRAM_GET_ME",
        "BOT_IDENTITY",
        "TELEGRAM_GET_CHAT",
        "PRIVATE_OWNER",
        "OUTBOX_RESERVATION",
        "TELEGRAM_SEND_VIDEO",
        "ACK_VALIDATION",
        "ACK_PERSISTENCE",
        "LOCAL_FINALIZATION",
        "RECEIPT_PERSISTENCE",
    }
)
REJECTIONS = frozenset(
    {
        "INVALID_TEST_TOKEN",
        "INVALID_PRIVATE_CHAT_ID",
        "BOT_IDENTITY_MISMATCH",
        "UNVERIFIED_PRIVATE_OWNER",
        "UNVERIFIED_TELEGRAM_ACK",
        "BOT_ID_USED_AS_CHAT",
    }
)
REJECTION_MESSAGES = {
    "INVALID_TEST_TOKEN": "Invalid test credentials",
    "INVALID_PRIVATE_CHAT_ID": "Invalid test credentials",
    "BOT_IDENTITY_MISMATCH": "Wrong test bot",
    "UNVERIFIED_PRIVATE_OWNER": "Unverified private owner",
    "UNVERIFIED_TELEGRAM_ACK": "Unverified Telegram acknowledgement",
    "BOT_ID_USED_AS_CHAT": "Bot destination is not an approved human recipient",
}


def number(value: object, low: int, high: int) -> int | None:
    return value if type(value) is int and low <= value <= high else None


class TelegramRejected(RuntimeError):
    def __init__(
        self, status: object, error_code: object = None, classification: object = None
    ) -> None:
        super().__init__("Telegram rejected the request")
        self.http_status = number(status, 100, 599)
        self.telegram_error_code = number(error_code, 100, 599)
        self.classification = safe_classification(classification)


@dataclass
class Diagnostic:
    stage: str = "DELIVERY_VALIDATION"
    network_started: bool = False
    send_video_started: bool = False
    telegram_ack_validated: bool = False
    durable_ack_confirmed: bool = False
    message_id: int | None = None
    error_code: str | None = None
    http_status: int | None = None
    telegram_error_code: int | None = None
    telegram_rejection: str | None = None
    prior_unknown: bool = False
    evidence_write_failed: bool = False


_current: ContextVar[Diagnostic | None] = ContextVar("status_send_diagnostic", default=None)


@contextmanager
def operation() -> Iterator[Diagnostic]:
    """Nested workflow/delivery calls share evidence; separate threads do not."""
    existing = _current.get()
    if existing is not None:
        yield existing
        return
    value = Diagnostic()
    reset = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(reset)


def stage(value: str) -> None:
    if value not in STAGES:
        raise ValueError("Unknown diagnostic stage")
    current = _current.get()
    if current is not None:
        current.stage = value


def reject(code: str) -> NoReturn:
    if code not in REJECTIONS:
        raise ValueError("Unknown diagnostic rejection")
    current = _current.get()
    if current is not None:
        current.error_code = code
    raise ValueError(REJECTION_MESSAGES[code])


def record_error(error: BaseException) -> None:
    """Never copy exception text, arbitrary class names, URLs or API bodies."""
    current = _current.get()
    if current is None or current.error_code is not None:
        return
    if isinstance(error, TelegramRejected):
        current.error_code = "TELEGRAM_API_REJECTED"
        current.http_status = error.http_status
        current.telegram_error_code = error.telegram_error_code
        current.telegram_rejection = safe_classification(error.classification)
    elif isinstance(error, TimeoutError):
        current.error_code = "TIMEOUT"
    elif isinstance(error, socket.gaierror):
        current.error_code = "DNS_ERROR"
    elif isinstance(error, ssl.SSLError):
        current.error_code = "TLS_ERROR"
    elif isinstance(error, ConnectionError):
        current.error_code = "CONNECTION_ERROR"
    elif isinstance(error, sqlite3.Error):
        current.error_code = "DATABASE_ERROR"
    else:
        from studio_core.jobs import LeaseLost

        if isinstance(error, LeaseLost):
            current.error_code = "LEASE_OR_OWNER_REJECTED"
        elif isinstance(error, ValueError):
            current.error_code = "VALIDATION_REJECTED"
        elif isinstance(error, KeyError):
            current.error_code = "REQUIRED_INPUT_MISSING"
        elif isinstance(error, OSError):
            current.error_code = "IO_ERROR"
        else:
            current.error_code = "INTERNAL_ERROR"


def telegram_call(
    transport: Any, token: str, method: str, body: bytes = b"", kind: str = "application/json"
) -> Any:
    stages = {
        "getMe": "TELEGRAM_GET_ME",
        "getChat": "TELEGRAM_GET_CHAT",
        "sendVideo": "TELEGRAM_SEND_VIDEO",
    }
    stage(stages[method])
    current = _current.get()
    if current is not None:
        # Before DNS/TCP/TLS/HTTP, not after a response. Failures remain unknown.
        current.network_started = True
        if method == "sendVideo":
            current.send_video_started = True
    try:
        return transport(token, method, body, kind)
    except Exception as error:
        record_error(error)
        raise


def acknowledge(message_id: object = None, *, durable: bool = False) -> None:
    current = _current.get()
    if current is not None:
        value = number(message_id, 1, 2**63 - 1)
        if value is not None:
            current.message_id = value
        if current.message_id is not None:
            current.telegram_ack_validated = True
            if durable:
                current.durable_ack_confirmed = True


def field(value: object, pattern: str) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def payload(env: Mapping[str, str], current: Diagnostic | None = None) -> dict[str, Any]:
    current = current or _current.get()
    # Without an operation context there is no proof that networking did not start.
    current = current or Diagnostic(network_started=True)
    state = (
        "ACK_CONFIRMED_LOCAL_EVIDENCE_PENDING"
        if current.durable_ack_confirmed
        else "UNKNOWN"
        if current.network_started or current.prior_unknown
        else "NOT_SENT_PRE_NETWORK"
    )
    return {
        "schema": 2,
        "delivery": "PUBLISHED" if current.durable_ack_confirmed else "SEND_REJECTED_OR_UNKNOWN",
        "state": state,
        "stage": current.stage,
        "error_code": current.error_code or "INTERNAL_ERROR",
        "network_started": current.network_started,
        "send_video_started": current.send_video_started,
        "telegram_ack_validated": current.telegram_ack_validated,
        "durable_ack_confirmed": current.durable_ack_confirmed,
        "message_id": current.message_id,
        "http_status": current.http_status,
        "telegram_error_code": current.telegram_error_code,
        "telegram_rejection": (
            safe_classification(current.telegram_rejection)
            if current.telegram_rejection is not None
            else None
        ),
        "prior_unknown": current.prior_unknown,
        "evidence_write_failed": current.evidence_write_failed,
        "run_id": field(env.get("GITHUB_RUN_ID"), r"[1-9][0-9]{0,19}"),
        "video_sha": field(env.get("VIDEO_SHA"), r"[0-9a-f]{64}"),
        "content_hash": field(env.get("CONTENT_HASH"), r"[0-9a-f]{64}"),
        "intent_id": field(env.get("SEND_INTENT_ARTIFACT_ID"), r"[1-9][0-9]{0,19}"),
        "retry": "NEVER_AUTOMATIC",
    }


def _atomic(path: Path, value: object) -> None:
    from .telegram_egress import atomic_json

    atomic_json(path, value)


def failure_receipt(path: str | Path, env: Mapping[str, str]) -> Any:
    """Keep prior ACK/unknown proof intact; write the latest diagnostic separately."""
    current = _current.get()
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Unsafe evidence path")
    path.parent.mkdir(parents=True, exist_ok=True)
    preserve = False
    prior: Any = None
    if path.exists():
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(prior, dict):
                raise ValueError("Invalid prior evidence")
            published = (
                prior.get("delivery") == "PUBLISHED"
                or prior.get("receipt", {}).get("delivery") == "PUBLISHED"
            )
            unknown = (
                prior.get("state") in {"UNKNOWN", "ACK_CONFIRMED_LOCAL_EVIDENCE_PENDING"}
                or prior.get("delivery") == "DELIVERY_UNKNOWN"
                or (
                    prior.get("delivery") == "SEND_REJECTED_OR_UNKNOWN"
                    and prior.get("state") != "NOT_SENT_PRE_NETWORK"
                )
            )
            preserve = published or unknown
            if current is not None:
                if unknown:
                    current.prior_unknown = True
                if published:
                    proof = prior.get("receipt", prior)
                    prior_sha = proof.get("video_sha256", proof.get("video_sha"))
                    approved_sha = field(env.get("VIDEO_SHA"), r"[0-9a-f]{64}")
                    if (
                        approved_sha is not None
                        and prior_sha == approved_sha
                        and number(proof.get("message_id"), 1, 2**63 - 1) is not None
                    ):
                        acknowledge(proof.get("message_id"), durable=True)
                    else:
                        current.prior_unknown = True
        except (OSError, ValueError, TypeError, AttributeError):
            # Corrupt evidence is not evidence of an unsent operation.
            preserve = True
            if current is not None:
                current.prior_unknown = True
    value = payload(env)
    _atomic(path.with_name("send-diagnostics.json"), value)
    if not preserve:
        _atomic(path, value)
    return prior if preserve and prior is not None else value


def attach_error(error: Any, env: Mapping[str, str]) -> None:
    try:
        error.status_diagnostics = payload(env)
    except Exception:
        pass
