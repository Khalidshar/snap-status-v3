"""Status Bot V2 runtime entrypoint.

Manual publishing is the safe default. Automatic WhatsApp Status publishing must
remain behind the explicit kill switch until its readiness gate passes.
"""

from studio_core.jobs import Job, Priority


def create_job(requested_by: str, content_type: str = "status") -> Job:
    return Job(
        project="status",
        request_type="whatsapp_or_schedule",
        content_type=content_type,
        destination="whatsapp_status",
        requested_by=requested_by,
        priority=Priority.NORMAL,
    )
