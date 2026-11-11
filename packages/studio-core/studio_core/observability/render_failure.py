"""Bounded render diagnostics: fixed vocabularies, never argv or raw stderr."""

import subprocess
from pathlib import Path
from typing import Any

from .failure_code import failure_code

TOOLS = {"FFMPEG", "FFPROBE", "NODE", "CHROMIUM", "SYSTEMCTL", "OTHER"}
REASONS = {
    "NO_SPACE",
    "PERMISSION",
    "MISSING_DEPENDENCY",
    "MEMORY",
    "INVALID_MEDIA",
    "BROWSER_START",
    "SIGNAL",
    "EXIT_STATUS",
    "TIMEOUT",
    "UNKNOWN",
}
CODES = {
    "NO_FRESH_CONTENT",
    "SUBPROCESS_FAILED",
    "TIMEOUT",
    "PERMISSION_DENIED",
    "FILE_MISSING",
    "IMPORT_FAILED",
    "OS_ERROR",
    "VALIDATION_FAILED",
    "UNEXPECTED_FAILURE",
}


def validate(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"code", "tool", "exit", "reason"}
        or not isinstance(value["code"], str)
        or value["code"] not in CODES
        or not isinstance(value["tool"], str)
        or value["tool"] not in TOOLS
        or not isinstance(value["reason"], str)
        or value["reason"] not in REASONS
        or type(value["exit"]) is not int
        or not -255 <= value["exit"] <= 255
    ):
        raise ValueError("RENDER_DIAGNOSTIC_INVALID")
    return dict(value)


class RenderWorkerFailure(subprocess.CalledProcessError):
    def __init__(self, value: Any) -> None:
        self.diagnostic = validate(value)
        super().__init__(self.diagnostic["exit"] or 1, ["render-worker"])


def diagnostic(error: Exception) -> dict[str, Any]:
    if isinstance(error, RenderWorkerFailure):
        return validate(error.diagnostic)
    tool, status, reason = "OTHER", 0, "UNKNOWN"
    if isinstance(error, subprocess.CalledProcessError):
        status = error.returncode if type(error.returncode) is int else 0
        status = status if -255 <= status <= 255 else 0
        command = error.cmd
        executable = command[0] if isinstance(command, (tuple, list)) and command else ""
        executable = Path(executable).name if isinstance(executable, str) else ""
        tool = {
            "ffmpeg": "FFMPEG",
            "ffprobe": "FFPROBE",
            "node": "NODE",
            "chromium": "CHROMIUM",
            "chrome": "CHROMIUM",
            "systemctl": "SYSTEMCTL",
        }.get(executable, "OTHER")
        reason = "SIGNAL" if status < 0 else "EXIT_STATUS"
        raw = error.stderr
        text = (
            raw[:8192].decode("utf-8", "replace")
            if isinstance(raw, bytes)
            else (raw[:8192] if isinstance(raw, str) else "")
        )
        text = text.lower()
        for needle, code in (
            ("no space left on device", "NO_SPACE"),
            ("permission denied", "PERMISSION"),
            ("cannot find module", "MISSING_DEPENDENCY"),
            ("error while loading shared libraries", "MISSING_DEPENDENCY"),
            ("out of memory", "MEMORY"),
            ("invalid data found when processing input", "INVALID_MEDIA"),
            ("failed to launch", "BROWSER_START"),
        ):
            if needle in text:
                reason = code
                break
    elif isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        reason = "TIMEOUT"
    elif isinstance(error, PermissionError):
        reason = "PERMISSION"
    elif isinstance(error, (FileNotFoundError, ImportError)):
        reason = "MISSING_DEPENDENCY"
    return validate({"code": failure_code(error), "tool": tool, "exit": status, "reason": reason})


def fields(error: Exception) -> str:
    value = diagnostic(error)
    return f"tool={value['tool']} exit={value['exit']} reason={value['reason']}"
