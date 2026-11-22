"""Safe diagnostic codes for service logs."""

import subprocess

from studio_core.production.fashion_policy import FashionUnavailable

NO_FRESH_CONTENT_MESSAGE = "لا يوجد خبر عاجل الآن"
NO_FRESH_FASHION_MESSAGE = "لا يوجد خبر أناقة جديد الآن"
WORKING_MESSAGE = "⏳ جاري صنع «{label}»… يأخذ عادةً دقيقتين إلى أربع."
# Owner 21 Nov 2026: news and «يومك في دقيقة» reach the owner only through the production line
# (auditor + the 18 agents). The pre-production paths stay in the code, locked.
LEGACY_LOCKED_MESSAGE = (
    "«{label}» ما ينصنع إلا من خط الإنتاج (المدقق والوكلاء الـ١٨)، وخط الإنتاج موقوف"
    " في هذا التشغيل. ما انرسل شي."
)


class NoFreshContent(ValueError):
    """No eligible current bulletin; never substitute fabricated or stale news."""

    def __init__(self) -> None:
        super().__init__("NO_FRESH_CONTENT")


def failure_reason(error: Exception) -> str:
    """The internal reason code (e.g. LEGACY_BULLETIN_INCOMPLETE) when the message is exactly
    one; never free text, paths or arguments (owner 22 Nov 2026: know why a clip failed)."""
    import re

    text = str(error)
    return text if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", text) else ""


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
