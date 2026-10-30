"""Read-only reachability audit over Network Core; no headlines leave this module."""

import hashlib
from datetime import UTC, datetime
from typing import Any, Protocol

from studio_core.network.client import NetworkError, NetworkResponse
from studio_core.sources import for_command
from studio_core.sources.feeds import parse_feed


class Client(Protocol):
    def get(self, url: str) -> NetworkResponse: ...


def audit(command: str, client: Client) -> list[dict[str, Any]]:
    """One fetch per candidate feed. Failure is recorded, never retried or published.

    An audit never promotes a candidate, widens a host allowlist, follows a website
    instead of RSS, rewrites religious text, or implies a reuse/translation licence.
    The proxy enforces DNS, redirects, TLS, byte limits and per-app isolation.
    """
    result: list[dict[str, Any]] = []
    for source in for_command(command):
        if source.kind not in {"rss", "website"}:
            continue
        row: dict[str, Any] = {
            "source_id": source.id,
            "checked_at": datetime.now(UTC).isoformat(),
            "publication_enabled": False,
        }
        if source.status == "excluded":
            row["result"] = "EXCLUDED"
        elif source.kind != "rss":
            row["result"] = "NOT_A_FEED"
        else:
            try:
                response = client.get(source.url)
                if not 200 <= response.status < 300:
                    raise ValueError("Unsuccessful upstream response")
                digest = hashlib.sha256(response.body).hexdigest()
                if digest != response.sha256:
                    raise ValueError("Response integrity failed")
                items = parse_feed(
                    response.body,
                    {
                        "name": source.name,
                        "url": source.url,
                        "language": source.language,
                        "section": "news",
                    },
                    style_only=False,
                )
                row.update(
                    result="XML_VERIFIED" if items else "NO_VALID_DATED_HEADLINES",
                    http_status=response.status,
                    valid_items=len(items),
                    sha256=digest,
                )
            except (NetworkError, ValueError) as error:
                # Never expose raw URLs, proxy errors, article text or credentials.
                row.update(result="UNVERIFIED", error_type=type(error).__name__)
        result.append(row)
    return result
