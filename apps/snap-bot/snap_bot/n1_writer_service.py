"""Opt-in journal writer listener; existing bot services do not enable this module.
Network deadlines cannot interrupt fsync. A busy shutdown retains its socket path.
"""

import argparse
import collections
import json
import os
import re
import signal
import socket
import stat
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any, cast

from . import n1_ipc as ipc
from .n1_writer import IsolatedAppendHandler, PhysicalWriter


class ServiceRefused(RuntimeError):
    pass


def credential_access(fd: int, meta: os.stat_result) -> bool:
    if not meta.st_mode & 0o077:
        return True
    # systemd 255 grants a named UID read ACL; its mask appears as group bits.
    # Accept only root-owned 0440 with exactly owner/read, service/read,
    # owning group/none, mask/read and others/none. Never allow group readers.
    if meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) != 0o440:
        return False
    try:
        acl = os.getxattr(fd, "system.posix_acl_access")
    except OSError:
        return False
    if len(acl) != 44 or struct.unpack("<I", acl[:4])[0] != 2:
        return False
    entries = [struct.unpack("<HHI", acl[i : i + 8]) for i in range(4, len(acl), 8)]
    expected = {
        (1, 4, 0xFFFFFFFF),
        (2, 4, os.geteuid()),
        (4, 0, 0xFFFFFFFF),
        (16, 4, 0xFFFFFFFF),
        (32, 0, 0xFFFFFFFF),
    }
    return len(set(entries)) == 5 and set(entries) == expected


def credential(name: object) -> bytes:
    if not isinstance(name, str) or not re.fullmatch("[A-Za-z0-9_-]{1,64}", name):
        raise ServiceRefused("CREDENTIAL_NAME")
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory or not Path(directory).is_absolute():
        raise ServiceRefused("CREDENTIAL_DIRECTORY")
    path = Path(directory) / name
    if Path(directory).is_symlink():
        raise ServiceRefused("CREDENTIAL_DIRECTORY")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        meta = os.fstat(fd)
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_nlink != 1
            or not credential_access(fd, meta)
            or meta.st_uid not in (0, os.geteuid())
            or not 32 <= meta.st_size <= 4096
        ):
            raise ServiceRefused("CREDENTIAL_PERMISSIONS")
        key = os.read(fd, 4097)
        if len(key) != meta.st_size:
            raise ServiceRefused("CREDENTIAL_SIZE")
        return key
    finally:
        os.close(fd)


class WriterService:
    def __init__(
        self,
        path: str | os.PathLike[str],
        worker_uid: int,
        app: str,
        handler: Callable[[Any], Any],
        *,
        max_connections: int = 1,
        rate_limit: int = 20,
        rate_window: float = 60,
        network_timeout: float = 2,
    ) -> None:
        self.path = Path(path)
        self.worker_uid = ipc.uid(worker_uid)
        ipc.identity(app, "service")
        self.app = app
        self.handler = handler
        if type(max_connections) is not int or not 1 <= max_connections <= 4:
            raise ValueError("Invalid concurrency")
        if (
            type(rate_limit) is not int
            or not 1 <= rate_limit <= 1000
            or not 0 < rate_window <= 3600
        ):
            raise ValueError("Invalid rate limit")
        ipc.budget(network_timeout)
        self.max_connections = max_connections
        self.rate_limit = rate_limit
        self.rate_window = rate_window
        self.network_timeout = network_timeout
        self.listener: socket.socket | None = None
        self.inode: int | None = None
        self._lock = threading.Lock()
        self._threads: set[threading.Thread] = set()
        self._times: collections.deque[float] = collections.deque()
        self.stats = {
            "admitted": 0,
            "peer_rejected": 0,
            "busy_rejected": 0,
            "rate_rejected": 0,
            "protocol_errors": 0,
        }

    def start(self) -> None:
        parent = self.path.parent
        if not self.path.is_absolute() or parent.is_symlink():
            raise ServiceRefused("SOCKET_DIRECTORY")
        meta = parent.stat()
        if (
            not stat.S_ISDIR(meta.st_mode)
            or meta.st_uid not in (0, os.geteuid())
            or meta.st_mode & 0o022
        ):
            raise ServiceRefused("SOCKET_DIRECTORY_PERMISSIONS")
        # Never unlink a pre-existing socket or infer an old writer is dead.
        if self.path.exists() or self.path.is_symlink():
            raise ServiceRefused("SOCKET_ALREADY_EXISTS")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            self.inode = self.path.lstat().st_ino
            os.chmod(self.path, 0o660)
            listener.listen(8)
            listener.settimeout(0.2)
        except Exception:
            listener.close()
            raise
        self.listener = listener

    def _serve(self, conn: socket.socket) -> None:
        try:
            ipc.serve_connection(
                conn,
                worker_uid=self.worker_uid,
                app=self.app,
                handler=self.handler,
                timeout=self.network_timeout,
            )
        except Exception:
            # Fixed counters only: never log request bodies or exception text.
            with self._lock:
                self.stats["protocol_errors"] += 1
        finally:
            conn.close()
            with self._lock:
                self._threads.discard(threading.current_thread())

    def accept_one(self) -> None:
        try:
            conn, _ = cast(socket.socket, self.listener).accept()
        except TimeoutError:
            return
        try:
            ipc.authenticate(conn, self.worker_uid)
        except ipc.PeerDenied:
            self.stats["peer_rejected"] += 1
            conn.close()
            return
        with self._lock:
            now = time.monotonic()
            while self._times and self._times[0] <= now - self.rate_window:
                self._times.popleft()
            if len(self._threads) >= self.max_connections:
                self.stats["busy_rejected"] += 1
                conn.close()
                return
            if len(self._times) >= self.rate_limit:
                self.stats["rate_rejected"] += 1
                conn.close()
                return
            self._times.append(now)
            self.stats["admitted"] += 1
            thread = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            self._threads.add(thread)
        thread.start()

    def run(self, stop: threading.Event) -> None:
        if self.listener is None:
            raise ServiceRefused("SERVICE_NOT_STARTED")
        while not stop.is_set():
            self.accept_one()

    def shutdown(self, timeout: float = 5) -> None:
        if not 0 <= timeout <= 30:
            raise ValueError("Invalid shutdown timeout")
        if self.listener:
            self.listener.close()
            self.listener = None
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                threads = list(self._threads)
            if not threads:
                break
            left = deadline - time.monotonic()
            if left <= 0:
                raise ServiceRefused("WRITER_BUSY_SOCKET_RETAINED")
            for thread in threads:
                thread.join(min(left, 0.1))
        if self.inode is not None:
            meta = self.path.lstat()
            if (
                not stat.S_ISSOCK(meta.st_mode)
                or meta.st_ino != self.inode
                or meta.st_uid != os.geteuid()
            ):
                raise ServiceRefused("SOCKET_IDENTITY_CHANGED")
            self.path.unlink()
            self.inode = None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--enable-service", action="store_true", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--journal-root", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--worker-uid", type=int, required=True)
    parser.add_argument("--credential-name", default="journal-key")
    args = parser.parse_args()
    # Key material never appears in argv, output or connection request fields.
    writer = PhysicalWriter(Path(args.journal_root), credential(args.credential_name), args.app)
    service = WriterService(args.socket, args.worker_uid, args.app, IsolatedAppendHandler(writer))
    stop = threading.Event()

    def terminate(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    service.start()
    print(json.dumps({"status": "READY", "app": args.app}), flush=True)
    try:
        service.run(stop)
    finally:
        service.shutdown()


if __name__ == "__main__":
    main()
