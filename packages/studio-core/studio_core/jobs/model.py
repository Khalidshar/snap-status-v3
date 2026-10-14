from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RESEARCHING = "RESEARCHING"
    PROCESSING = "PROCESSING"
    DOWNLOADING = "DOWNLOADING"
    DESIGNING = "DESIGNING"
    RENDERING = "RENDERING"
    VALIDATING = "VALIDATING"
    REPAIRING = "REPAIRING"
    DELIVERING = "DELIVERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    QUARANTINED = "QUARANTINED"


class Priority(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    BACKGROUND = "BACKGROUND"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Job:
    project: str
    request_type: str
    content_type: str
    destination: str
    requested_by: str
    priority: Priority = Priority.NORMAL
    job_id: str = field(default_factory=lambda: str(uuid4()))
    idempotency_key: str | None = None
    status: JobStatus = JobStatus.QUEUED
    current_stage: str = "created"
    created_at: datetime = field(default_factory=utcnow)
    expires_at: datetime | None = None
    attempts: int = 0
    errors: list[str] = field(default_factory=list)

    def transition(self, status: JobStatus, stage: str | None = None) -> None:
        self.status = status
        if stage:
            self.current_stage = stage
