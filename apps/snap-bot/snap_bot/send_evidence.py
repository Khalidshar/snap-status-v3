"""Conservative network boundary and durable, secret-free failure evidence."""

from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any

network_started: ContextVar[bool] = ContextVar("telegram_network_started", default=False)


def tracked_transport(transport: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(transport)
    def call(*args: Any, **kwargs: Any) -> Any:
        # Mark before DNS/TLS/HTTP: even a failed first request remains UNKNOWN.
        network_started.set(True)
        return transport(*args, **kwargs)

    return call


def failure_receipt(
    path: str | Path,
    *,
    run_id: object,
    video_sha: Any,
    intent_id: object = None,
    content_hash: Any = None,
    reason: str,
) -> Any:
    from .telegram_egress import atomic_json

    value = {
        "schema": 1,
        "run_id": str(run_id),
        "video_sha": video_sha,
        "state": "UNKNOWN" if network_started.get() else "NOT_SENT_PRE_NETWORK",
        "reason": reason,
        "intent_artifact_id": str(intent_id) if intent_id else None,
        "content_hash": content_hash,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        import json

        try:
            prior = json.loads(path.read_text())
        except Exception:
            prior = {"state": "UNKNOWN"}
        # Never downgrade evidence of an earlier invocation of this same intent.
        if prior.get("receipt", {}).get("delivery") == "PUBLISHED":
            return prior
        if prior.get("state") != "NOT_SENT_PRE_NETWORK":
            value["state"] = "UNKNOWN"
            value["reason"] = "PRIOR_OUTCOME_NOT_PROVEN_UNSENT"

    atomic_json(path, value)
    import os

    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return value


def delivery_evidence(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def guarded(
        config: Any,
        job_id: str,
        reviewed_sha: str,
        transport: Any = None,
        lease_token: str | None = None,
        review_mode: str | None = None,
        **kwargs: Any,
    ) -> Any:
        from studio_core.storage import Storage

        from .telegram_egress import api

        reset = network_started.set(False)
        try:
            return function(
                config,
                job_id,
                reviewed_sha,
                transport=tracked_transport(api if transport is None else transport),
                lease_token=lease_token,
                review_mode=review_mode,
                **kwargs,
            )
        except Exception:
            environ = __import__("os").environ
            if kwargs.get("channel") == "main":
                names, missing = ("TG_TOKEN", "TG_CHAT"), "INVALID_MAIN_CREDENTIALS"
            else:
                names, missing = ("TG_TEST_TOKEN", "TG_TEST_CHAT"), "INVALID_TEST_CREDENTIALS"
            failure_receipt(
                Storage(config).workspace(job_id) / "output/pre-network-receipt.json",
                run_id=environ.get("GITHUB_RUN_ID", ""),
                video_sha=reviewed_sha,
                reason=missing
                if not all(environ.get(name) for name in names)
                else "DELIVERY_REJECTED_OR_UNKNOWN",
            )
            raise
        finally:
            started = network_started.get()
            network_started.reset(reset)
            if started:
                network_started.set(True)

    return guarded
