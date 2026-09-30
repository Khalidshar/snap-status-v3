import re
from dataclasses import dataclass


class UnauthorizedCommand(PermissionError):
    pass


@dataclass(frozen=True)
class Allowlist:
    identities: frozenset[str]

    def require(self, identity: str) -> None:
        if identity not in self.identities:
            raise UnauthorizedCommand("sender is not authorized")


_SECRET_PATTERNS = (
    re.compile(r"(?i)(token|secret|api[_-]?key|password)\s*[=:]\s*([^\s,;]+)"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b"),
)


def redact_secrets(message: str) -> str:
    value = message
    value = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}=[REDACTED]", value)
    value = _SECRET_PATTERNS[1].sub("[REDACTED_TOKEN]", value)
    return value
