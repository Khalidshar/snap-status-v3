from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Any


def text_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


@dataclass
class ContentPackage:
    id: str
    type: str
    title: str
    body: str
    language: str
    source: str
    source_url: str
    destination: str
    sacred: bool = False
    verified: bool = False
    locked: bool = False
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return text_hash(f"{self.title}\n{self.body}")

    def sacred_lock(self, reference_hash: str) -> None:
        if not self.sacred:
            return
        if text_hash(self.body) != reference_hash:
            raise ValueError("Sacred content does not match independent reference")
        self.verified = True
        self.locked = True

    def replace_body(self, value: str) -> None:
        if self.locked:
            raise RuntimeError("LOCKED_TEXT cannot be modified")
        self.body = value
