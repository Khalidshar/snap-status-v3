"""Opt-in generation-fenced journal daemon; no bot or recovery activation."""

import argparse
import json
import os
import signal
import threading
import time
from pathlib import Path

from . import n1_ipc as ipc
from .n1_control import ControlHandler, ControlService
from .n1_generations import RegisteredWriter
from .n1_writer import IsolatedAppendHandler, PhysicalWriter
from .n1_writer_service import ServiceRefused, WriterService, credential


class JournalService:
    def __init__(self, append_path, control_path, worker_uid, authority_uid, writer, app):
        worker_uid = ipc.uid(worker_uid)
        authority_uid = ipc.uid(authority_uid)
        if (
            0 in (worker_uid, authority_uid, os.geteuid())
            or len({worker_uid, authority_uid, os.geteuid()}) != 3
        ):
            raise ServiceRefused("JOURNAL_ROLE_SEPARATION")
        append_path = Path(append_path)
        control_path = Path(control_path)
        if (
            not append_path.is_absolute()
            or not control_path.is_absolute()
            or append_path == control_path
        ):
            raise ServiceRefused("JOURNAL_SOCKET_SEPARATION")
        if writer.app != app:
            raise ServiceRefused("JOURNAL_APP_BINDING")
        registered = RegisteredWriter(writer)
        self.append = WriterService(append_path, worker_uid, app, IsolatedAppendHandler(registered))
        self.control = ControlService(
            control_path, authority_uid, app, ControlHandler(registered.authority)
        )
        self.planes = (self.append, self.control)
        self.threads = []
        self.failures = []
        self.lock = threading.Lock()
        self.started = False

    def start(self):
        if self.started:
            raise ServiceRefused("JOURNAL_ALREADY_STARTED")
        try:
            for plane in self.planes:
                plane.start()
        except Exception:
            errors = []
            for plane in reversed(self.planes):
                try:
                    plane.shutdown()
                except Exception:
                    errors.append(True)
            if errors:
                raise ServiceRefused("JOURNAL_STARTUP_SOCKET_RETAINED") from None
            raise
        self.started = True

    def _listen(self, plane, stop):
        try:
            plane.run(stop)
        except Exception:
            with self.lock:
                self.failures.append(True)
            stop.set()
        else:
            if not stop.is_set():
                with self.lock:
                    self.failures.append(True)
                stop.set()

    def run(self, stop):
        if not self.started:
            raise ServiceRefused("JOURNAL_NOT_STARTED")
        if self.threads:
            raise ServiceRefused("JOURNAL_ALREADY_RUN")
        self.threads = [
            threading.Thread(target=self._listen, args=(plane, stop)) for plane in self.planes
        ]
        for thread in self.threads:
            thread.start()
        while not stop.wait(0.1):
            pass
        for thread in self.threads:
            thread.join(1)
        if self.failures:
            raise ServiceRefused("JOURNAL_PLANE_FAILED")

    def shutdown(self, timeout=5):
        if not 0 <= timeout <= 30:
            raise ValueError("Invalid shutdown timeout")
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            raise ServiceRefused("JOURNAL_LISTENER_BUSY")
        errors = []
        for plane in reversed(self.planes):
            try:
                plane.shutdown(max(0, deadline - time.monotonic()))
            except Exception:
                errors.append(True)
        if errors:
            raise ServiceRefused("JOURNAL_BUSY_SOCKET_RETAINED")
        self.started = False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--enable-journal", action="store_true", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--journal-root", type=Path, required=True)
    parser.add_argument("--append-socket", type=Path, required=True)
    parser.add_argument("--control-socket", type=Path, required=True)
    parser.add_argument("--worker-uid", type=int, required=True)
    parser.add_argument("--authority-uid", type=int, required=True)
    parser.add_argument("--credential-name", default="journal-key")
    args = parser.parse_args()
    writer = PhysicalWriter(args.journal_root, credential(args.credential_name), args.app)
    service = JournalService(
        args.append_socket,
        args.control_socket,
        args.worker_uid,
        args.authority_uid,
        writer,
        args.app,
    )
    stop = threading.Event()

    def terminate(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    service.start()
    print(
        json.dumps({"status": "READY", "app": args.app, "planes": ["append", "control"]}),
        flush=True,
    )
    try:
        service.run(stop)
    finally:
        service.shutdown()


if __name__ == "__main__":
    main()
