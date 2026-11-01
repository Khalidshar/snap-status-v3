"""WhatsApp requested brief; prepared sources feed the existing twelve-agent delivery path."""

from typing import Any

from studio_core.sources.daily_brief import prepare, validate_brief

from . import content_clips
from .command_control import halted
from .service_window import ensure


def send(outbox: Any, transport: Any, news: Any, item: dict[str, Any], runtime: Any) -> bool:
    ensure(outbox)
    if halted():
        raise ValueError("WA_SEND_HALTED")
    value = prepare(news, item["tag"])
    if value is None:
        return False
    import time

    validate_brief(value, time.time())
    ensure(outbox)
    if halted():
        raise ValueError("WA_SEND_HALTED")
    outcome = "UNKNOWN"
    try:
        content_clips.send(outbox, transport, item, "brief", runtime, source=value)
        outcome = "ACK"
        return True
    finally:
        with news.connect() as db:
            event = "brief:" + item["tag"]
            db.execute(
                "UPDATE replies SET state=? WHERE event IN (?,?,?)",
                (outcome, event, event + ":saudi", event + ":tech"),
            )
