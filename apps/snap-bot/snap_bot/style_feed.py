"""Compatibility adapter for #52; parsing belongs to the shared source service."""

import json
from pathlib import Path
from typing import Any

from studio_core.sources.feeds import (
    MAX_BYTES as MAX_BYTES,
)
from studio_core.sources.feeds import (
    _source,
)
from studio_core.sources.feeds import (
    deduplicate as deduplicate,
)
from studio_core.sources.feeds import (
    parse_feed as parse_feed,
)
from studio_core.sources.feeds import (
    title_fingerprint as title_fingerprint,
)

DATA = Path(__file__).with_name("data") / "style_sources.json"


def sources() -> list[dict[str, Any]]:
    """Keep the original reviewed-only API; inventory candidates cannot enable it."""
    obj = json.loads(DATA.read_text(encoding="utf-8"))
    if obj.get("schema") != 1 or not isinstance(obj.get("sources"), list):
        raise ValueError("Invalid style source catalog")
    result = [_source(source) for source in obj["sources"]]
    if len({source["url"] for source in result}) != len(result):
        raise ValueError("Duplicate style source")
    return result
