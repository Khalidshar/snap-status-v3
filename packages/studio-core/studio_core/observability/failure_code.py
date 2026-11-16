"""Safe diagnostic codes for service logs."""

import subprocess

from studio_core.production.fashion_policy import FashionUnavailable

NO_FRESH_CONTENT_MESSAGE = "لا يوجد خبر عاجل الآن"
NO_FRESH_FASHION_MESSAGE = "لا يوجد خبر أناقة جديد الآن"


class NoFreshContent(ValueError):
    """No eligible current bulletin; never substitute fabricated or stale news."""

    def __init__(self) -> None:
        super().__init__("NO_FRESH_CONTENT")


def failure_code(error: Exception) -> str:
    """Classify failures without emitting exception messages or command arguments."""
    if isinstance(error, FashionUnavailable):
        return "FASHION_CONTENT_UNAVAILABLE"
    if isinstance(error, NoFreshContent):
        return "NO_FRESH_CONTENT"
    if isinstance(error, subprocess.CalledProcessError):
        return "SUBPROCESS_FAILED"
    if isinstance(error, (subprocess.TimeoutExpired, TimeoutError)):
        return "TIMEOUT"
    if isinstance(error, PermissionError):
        return "PERMISSION_DENIED"
    if isinstance(error, FileNotFoundError):
        return "FILE_MISSING"
    if isinstance(error, ImportError):
        return "IMPORT_FAILED"
    if isinstance(error, OSError):
        return "OS_ERROR"
    if isinstance(error, ValueError):
        return "VALIDATION_FAILED"
    return "UNEXPECTED_FAILURE"
