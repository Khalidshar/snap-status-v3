import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self.secrets = tuple(x for x in secrets if x)

    def clean(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: "[redacted]"
                if re.search(r"token|secret|password|authorization|api_key", k, re.I)
                else self.clean(v)
                for k, v in value.items()
            }
        if isinstance(value, (tuple, list)):
            return [self.clean(x) for x in value]
        if not isinstance(value, str):
            return value
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, "[redacted]")

        def clean_url(m: re.Match[str]) -> str:
            u = urlsplit(m.group(0))
            host = u.hostname or ""
            return urlunsplit((u.scheme, host, u.path, "[redacted]" if u.query else "", ""))

        return re.sub(r"https?://[^\s\"<>]+", clean_url, value)


def json_line(event: str, **fields: Any) -> str:
    return json.dumps({"event": event, **fields}, ensure_ascii=False, sort_keys=True)
