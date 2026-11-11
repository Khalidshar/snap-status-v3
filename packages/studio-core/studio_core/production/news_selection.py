"""Publisher diversity without weakening article, image or duplicate checks."""

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit


def publisher(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    # The BBC's feeds and article links use different registered domains.
    if host in {"bbc.com", "bbc.co.uk", "bbci.co.uk"} or host.endswith(
        (".bbc.com", ".bbc.co.uk", ".bbci.co.uk")
    ):
        return "bbc"
    return host


def diverse(
    candidates: list[dict[str, Any]], accepted: dict[str, int]
) -> Iterator[dict[str, Any]]:
    """Defer a publisher's third story until all alternatives have been tried.

    The caller counts only successfully validated stories. Failed images, expired
    stories and duplicate topics never consume another publisher's opportunity.
    """
    deferred = []
    for item in candidates:
        if accepted.get(publisher(item["url"]), 0) >= 2:
            deferred.append(item)
        else:
            yield item
    yield from deferred
