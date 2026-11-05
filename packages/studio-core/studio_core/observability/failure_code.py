"""Safe diagnostic codes for service logs."""

import subprocess


def failure_code(error: Exception) -> str:
    """Classify failures without emitting exception messages or command arguments."""
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
