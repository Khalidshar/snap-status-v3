"""Shared renderer primitives: isolated FFmpeg runs, glyph checks and atomic attempt files.

Graduated from the lab core 0.10.0 (snap-bot-v2 #51), where both applications
already use this single copy. Types added; the behaviour is unchanged. `fcntl`
stays a local import so the module still imports on non-Linux developer machines.
"""

import hashlib
import os
import re
import socket
import stat
import subprocess
import time
import uuid
from pathlib import Path


def run(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    # Callers pass a fixed tool path and generated arguments, never a shell string.
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)  # noqa: S603


def ensure_glyphs(text: str, charset: str) -> None:
    ranges = []
    for item in charset.split():
        ends = item.split("-")
        ranges.append((int(ends[0], 16), int(ends[-1], 16)))
    if any(not any(low <= ord(char) <= high for low, high in ranges) for char in text):
        raise ValueError("Font missing required glyph")


def proof(proxy_socket: str | Path) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    for name, family, kind in [
        ("ipv4", socket.AF_INET, socket.SOCK_STREAM),
        ("ipv6", socket.AF_INET6, socket.SOCK_STREAM),
        ("udp", socket.AF_INET, socket.SOCK_DGRAM),
    ]:
        try:
            with socket.socket(family, kind):
                checks[name] = False
        except OSError as exc:
            checks[name] = exc.errno in (1, 13, 97)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(proxy_socket))
        checks["egress_unreachable"] = False
    except OSError as exc:
        checks["egress_unreachable"] = exc.errno in (1, 2, 13)
    if not all(checks.values()):
        raise RuntimeError("Renderer network isolation missing")
    return checks


def write_once(path: str | Path, data: bytes) -> None:
    import fcntl

    path = Path(path)
    temporary = path.parent / (".tmp-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    # The lock protects even an old, slow writer from orphan cleanup.
    with os.fdopen(fd, "wb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                verify_once(path, data)
        finally:
            temporary.unlink(missing_ok=True)
    sync_dir(path.parent)


def verify_once(path: str | Path, data: bytes) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        meta = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_size != len(data)
            or source.read(len(data) + 1) != data
        ):
            raise ValueError("Attempt artifact immutable")
        os.fsync(source.fileno())


def cleanup_attempt_temps(
    root: str | Path, min_age: float = 3600, now: float | None = None
) -> None:
    """Unlink only old, unlocked regular .tmp-UUID files in this attempt."""
    import fcntl

    root = Path(root)
    if root.parent.name != "attempts" or not root.name.isdigit() or int(root.name) < 1:
        raise ValueError("Invalid cleanup attempt")
    if root.is_symlink():
        raise ValueError("Unsafe cleanup attempt")
    if not root.exists():
        return
    now = time.time() if now is None else now
    for _, _, names, directory in os.fwalk(root, follow_symlinks=False):
        removed = False
        for name in names:
            if not re.fullmatch(r"\.tmp-[0-9a-f]{32}", name):
                continue
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except OSError:
                continue
            try:
                meta = os.fstat(fd)
                if not stat.S_ISREG(meta.st_mode) or now - meta.st_mtime < min_age:
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (meta.st_dev, meta.st_ino):
                    continue
                os.unlink(name, dir_fd=directory)
                removed = True
            except FileNotFoundError:
                pass
            finally:
                os.close(fd)
        if removed:
            os.fsync(directory)


def sync_dir(path: str | Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def attempt_workspace(root: Path, attempt: int) -> Path:
    if type(attempt) is not int or attempt < 1:
        raise ValueError("Invalid render attempt")
    base = root / "attempts"
    for path in (base, base / str(attempt)):
        if path.is_symlink():
            raise ValueError("Unsafe attempt directory")
        path.mkdir(exist_ok=True, mode=0o750)
    for part in ("design", "temp", "output"):
        path = base / str(attempt) / part
        if path.is_symlink():
            raise ValueError("Unsafe attempt directory")
        path.mkdir(exist_ok=True, mode=0o700)
    cleanup_attempt_temps(base / str(attempt))
    return base / str(attempt)


def file_sha256(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(stream.fileno())
        if not stat.S_ISREG(meta.st_mode) or not 0 < meta.st_size <= 50_000_000:
            raise ValueError("Unsafe published artifact")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        os.fsync(stream.fileno())
    sync_dir(path.parent)
    return digest


def publish_once(partial: Path, final: Path) -> None:
    # Immutable within an attempt: never replace an artifact another call published.
    with open(partial, "rb") as stream:
        os.fsync(stream.fileno())
    try:
        os.link(partial, final)
    except FileExistsError:
        if final.is_symlink() or file_sha256(final) != file_sha256(partial):
            raise ValueError("Attempt artifact immutable") from None
    finally:
        partial.unlink()
    sync_dir(final.parent)
