"""Independent WhatsApp send stop marker with retained control evidence."""

import os
import uuid
from typing import Any

from .server_preview import STATE, safe_path

HALT = STATE / "HALT"
STOPPED = "الإرسال في واتساب موقوف، اكتب «شغّل» لإعادته."


def halted() -> bool:
    return safe_path(HALT).exists()


def change(stop: bool, runtime: Any, command_tag: str) -> str:
    path = safe_path(HALT)
    with runtime.locked():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        before = path.exists()
        runtime.append(
            "CONTROL_INTENT",
            action="halt" if stop else "unhalt",
            command_tag=command_tag,
        )
        if stop:
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        elif before:
            archive = safe_path(path.parent / "control-history")
            archive.mkdir(mode=0o700, exist_ok=True)
            os.rename(path, safe_path(archive / ("halt-" + uuid.uuid4().hex)))
            fd = os.open(archive, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        runtime.append(
            "CONTROL",
            action="halt" if stop else "unhalt",
            command_tag=command_tag,
            changed=before != stop,
        )
    if stop:
        return STOPPED
    return "تم تشغيل الإرسال في واتساب؛ لن تعاد الأوامر السابقة تلقائياً."


def status_text() -> str:
    return "إرسال واتساب: موقوف" if halted() else "إرسال واتساب: يعمل"
