"""The old factories' content and scene mechanism, behind an explicit v3 switch."""

import os


def enabled() -> bool:
    return os.environ.get("MADAR_LEGACY_PRODUCTION") == "1"
