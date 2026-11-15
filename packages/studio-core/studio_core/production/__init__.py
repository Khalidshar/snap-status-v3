"""The old factories' content and scene mechanism, behind an explicit v3 switch."""

import os

from .fashion_policy import KEYS as FASHION_KEYS


def enabled(kind: str | None = None) -> bool:
    return os.environ.get("MADAR_LEGACY_PRODUCTION") == "1" or kind in FASHION_KEYS
