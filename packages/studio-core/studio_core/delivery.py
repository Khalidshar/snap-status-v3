from dataclasses import dataclass
from enum import Enum


class OutboxState(str, Enum):
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    EXPIRED = "EXPIRED"
    MANUAL_SENT = "MANUAL_SENT"
    MANUAL_CONFIRMED = "MANUAL_CONFIRMED"


@dataclass
class Delivery:
    delivery_id: str
    job_id: str
    content_hash: str
    destination: str
    state: OutboxState = OutboxState.READY_TO_PUBLISH

    @property
    def idempotency_key(self) -> str:
        return f"{self.destination}:{self.content_hash}"


class IdempotencyRegistry:
    def __init__(self) -> None:
        self._completed: set[str] = set()

    def already_delivered(self, delivery: Delivery) -> bool:
        return delivery.idempotency_key in self._completed

    def mark_delivered(self, delivery: Delivery) -> None:
        self._completed.add(delivery.idempotency_key)
