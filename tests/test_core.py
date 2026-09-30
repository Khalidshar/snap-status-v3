import pytest

from studio_core.content import ContentPackage, text_hash
from studio_core.delivery import Delivery, IdempotencyRegistry
from studio_core.jobs import Job, JobStatus


def test_sacred_content_locks_only_after_reference_match():
    item = ContentPackage("1", "quran", "آية", "نص موثّق", "ar", "reference", "local", "snap", sacred=True)
    item.sacred_lock(text_hash("نص موثّق"))
    assert item.verified and item.locked
    with pytest.raises(RuntimeError):
        item.replace_body("تغيير ممنوع")


def test_sacred_mismatch_fails_closed():
    item = ContentPackage("1", "quran", "آية", "نص", "ar", "reference", "local", "status", sacred=True)
    with pytest.raises(ValueError):
        item.sacred_lock(text_hash("نص آخر"))


def test_job_transition():
    job = Job("snap", "command", "news", "telegram", "owner")
    job.transition(JobStatus.RUNNING, "research")
    assert job.status == JobStatus.RUNNING
    assert job.current_stage == "research"


def test_delivery_idempotency():
    delivery = Delivery("d1", "j1", "abc", "telegram")
    registry = IdempotencyRegistry()
    assert not registry.already_delivered(delivery)
    registry.mark_delivered(delivery)
    assert registry.already_delivered(delivery)
