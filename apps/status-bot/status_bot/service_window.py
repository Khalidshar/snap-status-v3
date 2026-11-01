"""Read-only WhatsApp service-window evidence, scoped to the pinned owner identity."""

import math
import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .whatsapp_contract import require

SECONDS = 86400


def status(repo: Any, binding: str) -> dict[str, Any]:
    require(repo.config.app == "status-bot-v2", "WA_APP_SCOPE_INVALID")
    now = repo.clock()
    try:
        with repo.connect() as db:
            row = db.execute(
                "SELECT last_inbound_at FROM whatsapp_windows WHERE identity_binding=?", (binding,)
            ).fetchone()
    except sqlite3.OperationalError:
        row = None
    last: Any = row[0] if row else None
    opened = (
        type(last) in (int, float)
        and math.isfinite(last)
        and type(now) in (int, float)
        and math.isfinite(now)
        and 0 <= now - last < SECONDS
    )
    return {
        "open": bool(opened),
        "state": "مفتوحة" if opened else "مقفلة",
        "expires_at": last + SECONDS
        if type(last) in (int, float) and math.isfinite(last)
        else None,
        "reason": None if opened else "WA_SERVICE_WINDOW_CLOSED",
    }


def ensure(outbox: Any) -> None:
    require(status(outbox.repo, outbox.identity.binding)["open"], "WA_SERVICE_WINDOW_CLOSED")


def text(outbox: Any) -> str:
    value = status(outbox.repo, outbox.identity.binding)
    if value["open"]:
        end = datetime.fromtimestamp(value["expires_at"], ZoneInfo("Asia/Riyadh"))
        label = f"{end.day}/{end.month}/{end.year}، {end.hour % 12 or 12}:{end.minute:02d} {'ص' if end.hour < 12 else 'م'}"
        return "نافذة واتساب: مفتوحة، تنتهي " + label.translate(
            str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
        )
    return "نافذة واتساب: مقفلة؛ لا توجد مهلة مراسلة سارية. أرسل رسالة من رقم خالد المثبّت لتجديد مهلة ٢٤ ساعة."
