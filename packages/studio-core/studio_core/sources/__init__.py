"""Command-to-source inventory shared by both apps. No network, text edits or sending.

References describe existing data; candidates are not runtime approvals. The app
owning a catalog remains responsible for its hashes, locks and render validation.
"""

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from studio_core.commands import BY_KEY, COMMANDS, INDEX, normalize

CATALOG = Path(__file__).with_name("catalog.json")
CONTENT_KEYS = frozenset(
    c.key for c in COMMANDS if c.section in {"news", "general", "faith", "brief"}
)
FAITH_KEYS = frozenset(c.key for c in COMMANDS if c.section == "faith")
KINDS = {"rss", "website", "catalog", "reference", "human_audio", "weather"}
STATUSES = {"candidate", "excluded", "reference"}
PERMISSIONS = {"unverified", "restricted", "headline_syndication", "reference_only"}


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    commands: tuple[str, ...]
    kind: str
    language: str
    status: str
    permission: str
    note: str
    evidence: str


def _text(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)
    ):
        raise ValueError("Invalid source text")
    return value


def _url(value: Any) -> str:
    url = _text(value)
    _text(unquote(url))
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or any(char.isspace() for char in url)
    ):
        raise ValueError("Invalid source URL")
    return url


def load(path: Path = CATALOG) -> tuple[Source, ...]:
    """Validate the entire inventory before exposing any binding. No app imports."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1:
        raise ValueError("Invalid source inventory")
    rows = data.get("sources")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Empty source inventory")
    result: list[Source] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(Source.__dataclass_fields__):
            raise ValueError("Invalid source fields")
        for key in ("id", "name", "kind", "language", "status", "permission", "note", "evidence"):
            _text(row[key])
        _url(row["url"])
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", row["id"])
            or row["kind"] not in KINDS
            or row["status"] not in STATUSES
            or row["language"] not in {"ar", "en", "und"}
            or row["permission"] not in PERMISSIONS
        ):
            raise ValueError("Invalid source metadata")
        bindings = row["commands"]
        if (
            not isinstance(bindings, list)
            or not bindings
            or any(not isinstance(key, str) or key not in CONTENT_KEYS for key in bindings)
            or len(bindings) != len(set(bindings))
        ):
            raise ValueError("Invalid command binding")
        if set(bindings) & FAITH_KEYS and row["kind"] not in {
            "catalog",
            "reference",
            "human_audio",
        }:
            raise ValueError("Editorial sources cannot supply sacred text")
        if row["status"] == "reference" and row["kind"] in {"rss", "website"}:
            raise ValueError("Unverified feed cannot become a reference")
        result.append(Source(**{**row, "commands": tuple(bindings)}))
    if len({s.id for s in result}) != len(result) or len({s.url for s in result}) != len(result):
        raise ValueError("Duplicate source")
    if set().union(*(set(s.commands) for s in result)) != CONTENT_KEYS:
        raise ValueError("Content command missing source inventory")
    return tuple(result)


def command_key(value: str) -> str:
    """Accept a registry key or the same exact name/number the bots already accept."""
    if value in BY_KEY:
        return value
    matched = INDEX.get(normalize(value))
    if matched:
        return matched.key
    raise ValueError("Unknown or ambiguous content command")


def for_command(value: str, *, catalog: tuple[Source, ...] | None = None) -> tuple[Source, ...]:
    key = command_key(value)
    inventory = load() if catalog is None else catalog
    return tuple(source for source in inventory if key in source.commands)


def plan(value: str, *, catalog: tuple[Source, ...] | None = None) -> dict[str, Any]:
    """Inspectable bindings, not proof that the bot implements or publishes a command."""
    key = command_key(value)
    sources = for_command(key, catalog=catalog)
    return {
        "command": key,
        "number": BY_KEY[key].number,
        "label": BY_KEY[key].label,
        "content": key in CONTENT_KEYS,
        "sacred_lock_required": key in FAITH_KEYS,
        "human_audio_required": key in FAITH_KEYS,
        "sources": [{**asdict(source), "commands": list(source.commands)} for source in sources],
        "live_feeds_enabled": False,
        "publication_enabled": False,
        "note": (
            "سجل ربط ومراجعة؛ لا يفعّل جلبًا أو رندرًا أو إرسالًا، ولا ينقل كتالوج السناب إلى الحالات."
        ),
    }
