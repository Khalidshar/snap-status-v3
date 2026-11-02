"""Read-only per-feed news freshness checks using the bots' NetworkClient."""

import time
from typing import Any

from studio_core.network.client import NetworkError
from studio_core.sources import for_command
from studio_core.sources.feeds import parse_feed
from studio_core.sources.news import TITLES, NewsUnavailable, collect, validate


class OneFeed:
    def __init__(self, url: str, response: Any) -> None:
        self.url, self.response = url, response

    def get(self, url: str) -> Any:
        if url != self.url:
            raise NetworkError("SOURCE_NOT_SELECTED")
        return self.response


def check(client: Any, now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    cache: dict[str, Any] = {}
    rows = []
    for key in TITLES:
        for source in for_command(key):
            if source.kind != "rss" or source.language != "ar" or source.status != "approved":
                continue
            row: dict[str, Any] = {
                "command": key,
                "source_id": source.id,
                "name": source.name,
                "url": source.url,
                "checked_at": now,
                "state": "متوقف",
            }
            try:
                if source.url not in cache:
                    try:
                        cache[source.url] = client.get(source.url)
                    except (NetworkError, OSError, ValueError) as error:
                        cache[source.url] = error
                response = cache[source.url]
                if isinstance(response, Exception):
                    raise NetworkError("SOURCE_FETCH_FAILED") from response
                row["http_status"] = response.status
                if response.status == 403:
                    row["state"] = "403"
                elif 200 <= response.status < 300:
                    parse_feed(
                        response.body,
                        {
                            "name": source.name,
                            "url": source.url,
                            "language": "ar",
                            "section": "news",
                        },
                        style_only=False,
                    )
                    try:
                        content = collect(key, OneFeed(source.url, response), now)["content"]
                        validate(content, now)
                        row["state"] = "يشتغل"
                    except NewsUnavailable:
                        row["state"] = "ما فيه خبر حديث"
            except (NetworkError, OSError, ValueError) as error:
                row["error_type"] = type(error).__name__
            rows.append(row)
    return rows
