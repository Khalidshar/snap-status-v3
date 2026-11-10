"""Diagnostics preserve actionable facts while dropping all private bytes."""

import json
import subprocess

import pytest

from studio_core.observability.render_failure import (
    RenderWorkerFailure,
    diagnostic,
    fields,
    validate,
)


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        (b"SECRET /private: No space left on device", "NO_SPACE"),
        ("SECRET: Permission denied", "PERMISSION"),
        ("SECRET: Cannot find module /private", "MISSING_DEPENDENCY"),
        ("SECRET: Out of memory", "MEMORY"),
        ("SECRET: Invalid data found when processing input", "INVALID_MEDIA"),
        ("SECRET: Failed to launch the browser", "BROWSER_START"),
        ("SECRET arbitrary text", "EXIT_STATUS"),
    ],
)
def test_only_fixed_diagnostic_values_cross_boundary(stderr, reason):
    error = subprocess.CalledProcessError(1, ["/private/ffmpeg", "SECRET"], stderr=stderr)
    value = diagnostic(error)
    assert value == {"code": "SUBPROCESS_FAILED", "tool": "FFMPEG", "exit": 1, "reason": reason}
    transported = RenderWorkerFailure(json.loads(json.dumps(value)))
    assert diagnostic(transported) == value
    assert "SECRET" not in fields(transported)
    assert "/private" not in fields(transported)


@pytest.mark.parametrize(
    "value",
    [
        {"code": "SECRET"},
        {"code": "SUBPROCESS_FAILED", "tool": "SECRET", "exit": 1, "reason": "EXIT_STATUS"},
        {"code": "SUBPROCESS_FAILED", "tool": "NODE", "exit": True, "reason": "EXIT_STATUS"},
    ],
)
def test_untrusted_worker_diagnostic_is_rejected(value):
    with pytest.raises(ValueError, match="RENDER_DIAGNOSTIC_INVALID"):
        validate(value)


def test_signal_timeout_and_non_subprocess_errors_are_safe():
    assert diagnostic(subprocess.CalledProcessError(-9, ["private"]))["reason"] == "SIGNAL"
    assert diagnostic(TimeoutError("SECRET"))["reason"] == "TIMEOUT"
    assert "SECRET" not in fields(ValueError("SECRET"))
