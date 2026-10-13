import json
import re
from urllib.parse import urlsplit, urlunsplit


class Redactor:
    def __init__(self, secrets=()):
        self.secrets = tuple(x for x in secrets if x)

    def clean(self, value):
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

        def clean_url(m):
            u = urlsplit(m.group(0))
            host = u.hostname or ""
            return urlunsplit((u.scheme, host, u.path, "[redacted]" if u.query else "", ""))

        return re.sub(r"https?://[^\s\"<>]+", clean_url, value)


def json_line(event, **fields):
    return json.dumps({"event": event, **fields}, ensure_ascii=False, sort_keys=True)
