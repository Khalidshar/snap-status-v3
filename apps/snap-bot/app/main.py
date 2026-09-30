"""Snap Bot V2 runtime entrypoint.

Telegram is the control/delivery surface. Snapchat publishing remains manual.
Legacy behavior is imported only through reviewed adapters after Discovery.
"""

from studio_core.jobs import Job, Priority


def create_job(requested_by: str, content_type: str = "news") -> Job:
    return Job(
        project="snap",
        request_type="telegram_command",
        content_type=content_type,
        destination="telegram",
        requested_by=requested_by,
        priority=Priority.HIGH,
    )
