import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from snap_bot import n1_ipc as ipc
from snap_bot.n1_contracts import Ledger
from snap_bot.n1_writer import generation
from snap_bot.n1_writer_service import ServiceRefused, WriterService, credential, credential_access
from studio_core.config import Config
from studio_core.jobs import Repository


@unittest.skipUnless(sys.platform == "linux", "Linux service acceptance")
class Service(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "writer.sock"
        self.running = []
        self.key = b"service-synthetic-key-000000000000000"

    def start(self, handler=None, **kwargs):
        service = WriterService(
            self.path,
            kwargs.pop("worker_uid", os.getuid()),
            "snap-bot-v2",
            handler or (lambda body: body),
            **kwargs,
        )
        service.start()
        stop = threading.Event()
        thread = threading.Thread(target=service.run, args=(stop,), daemon=True)
        thread.start()
        self.running.append((service, stop, thread))
        return service

    def tearDown(self):
        for service, stop, thread in self.running:
            stop.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            if service.inode is not None:
                service.shutdown(2)

    def call(self, body=None):
        return ipc.call(
            self.path,
            server_uid=os.getuid(),
            app="snap-bot-v2",
            request_id="service_1",
            envelope={} if body is None else body,
            timeout=2,
        )

    def creds(self):
        directory = self.root / "creds"
        directory.mkdir(mode=0o700)
        path = directory / "journal-key"
        path.write_bytes(self.key)
        path.chmod(0o600)
        return directory, path

    def test_idle_shutdown_removes_only_owned_socket(self):
        service = self.start()
        self.assertEqual(self.call({"synthetic": True}), {"synthetic": True})
        self.running[0][1].set()
        self.running[0][2].join(2)
        service.shutdown()
        self.assertFalse(self.path.exists())

    def test_preexisting_socket_is_not_removed_or_replaced(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as existing:
            existing.bind(str(self.path))
            inode = self.path.stat().st_ino
            with self.assertRaisesRegex(ServiceRefused, "SOCKET_ALREADY_EXISTS"):
                WriterService(self.path, os.getuid(), "snap-bot-v2", lambda b: b).start()
            self.assertEqual(self.path.stat().st_ino, inode)

    def test_writable_socket_directory_refused(self):
        self.root.chmod(0o777)
        with self.assertRaisesRegex(ServiceRefused, "SOCKET_DIRECTORY_PERMISSIONS"):
            WriterService(self.path, os.getuid(), "snap-bot-v2", lambda b: b).start()
        self.assertFalse(self.path.exists())

    def test_wrong_uid_is_rejected_without_handler(self):
        seen = []
        service = self.start(lambda b: seen.append(b), worker_uid=os.getuid() + 1)
        with self.assertRaises(ipc.RpcUncertain):
            self.call()
        deadline = time.monotonic() + 1
        while service.stats["peer_rejected"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(seen, [])
        self.assertEqual(service.stats["peer_rejected"], 1)

    def test_busy_limit_rejects_second_request_before_handler(self):
        gate = threading.Event()
        ready = threading.Event()
        seen = []
        results = []

        def handler(body):
            seen.append(body)
            ready.set()
            gate.wait(2)
            return body

        service = self.start(handler, max_connections=1)
        first = threading.Thread(target=lambda: results.append(self.call({"first": True})))
        first.start()
        try:
            self.assertTrue(ready.wait(1))
            with self.assertRaises(ipc.RpcUncertain):
                self.call({"second": True})
            # Socket EOF can reach the client before accept_one updates its counter.
            # Read under the writer's lock so the rejection has completed.
            with service._lock:
                self.assertEqual(service.stats["busy_rejected"], 1)
        finally:
            gate.set()
            first.join(2)
        self.assertEqual(seen, [{"first": True}])

    def test_rate_limit_counts_admitted_requests(self):
        service = self.start(rate_limit=1, rate_window=60)
        self.assertEqual(self.call(), {})
        deadline = time.monotonic() + 1
        while service._threads and time.monotonic() < deadline:
            time.sleep(0.01)
        with self.assertRaises(ipc.RpcUncertain):
            self.call()
        self.assertEqual(service.stats["rate_rejected"], 1)

    def test_busy_shutdown_retains_socket_until_writer_drains(self):
        gate = threading.Event()
        ready = threading.Event()
        results = []

        def handler(body):
            ready.set()
            gate.wait(2)
            return body

        service = self.start(handler)
        first = threading.Thread(target=lambda: results.append(self.call()))
        first.start()
        try:
            self.assertTrue(ready.wait(1))
            self.running[0][1].set()
            self.running[0][2].join(2)
            with self.assertRaisesRegex(ServiceRefused, "WRITER_BUSY_SOCKET_RETAINED"):
                service.shutdown(0)
            self.assertTrue(self.path.exists())
        finally:
            gate.set()
            first.join(2)
        service.shutdown(2)
        self.assertFalse(self.path.exists())

    def test_replacement_path_is_never_deleted_by_shutdown(self):
        service = self.start()
        self.running[0][1].set()
        self.running[0][2].join(2)
        self.path.unlink()
        self.path.write_text("replacement")
        with self.assertRaisesRegex(ServiceRefused, "SOCKET_IDENTITY_CHANGED"):
            service.shutdown()
        self.assertEqual(self.path.read_text(), "replacement")
        service.inode = None

    def test_partial_client_times_out_and_slot_becomes_available(self):
        service = self.start(network_timeout=0.15)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(self.path))
            client.sendall(b"\0")
            deadline = time.monotonic() + 1
            while service.stats["protocol_errors"] == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(service.stats["protocol_errors"], 1)
        self.assertEqual(self.call(), {})

    def test_credential_loaded_without_values_in_errors(self):
        directory, _path = self.creds()
        with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(directory)}):
            self.assertEqual(credential("journal-key"), self.key)

    def test_missing_credential_directory_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ServiceRefused, "CREDENTIAL_DIRECTORY"):
                credential("journal-key")

    def test_symlink_and_public_credentials_refused(self):
        directory, path = self.creds()
        link = directory / "linked-key"
        link.symlink_to(path)
        with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(directory)}):
            with self.assertRaises(OSError):
                credential("linked-key")
            path.chmod(0o644)
            with self.assertRaisesRegex(ServiceRefused, "CREDENTIAL_PERMISSIONS"):
                credential("journal-key")

    def test_oversized_credential_and_path_traversal_refused(self):
        directory, path = self.creds()
        path.write_bytes(b"x" * 4097)
        with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(directory)}):
            with self.assertRaisesRegex(ServiceRefused, "CREDENTIAL_PERMISSIONS"):
                credential("journal-key")
            with self.assertRaisesRegex(ServiceRefused, "CREDENTIAL_NAME"):
                credential("../key")

    def acl(self, entries):
        return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)

    def test_systemd_named_service_acl_is_not_a_group_grant(self):
        from types import SimpleNamespace

        entries = [
            (1, 4, 0xFFFFFFFF),
            (2, 4, os.geteuid()),
            (4, 0, 0xFFFFFFFF),
            (16, 4, 0xFFFFFFFF),
            (32, 0, 0xFFFFFFFF),
        ]
        with patch("os.getxattr", return_value=self.acl(entries)):
            self.assertTrue(credential_access(1, SimpleNamespace(st_mode=0o100440, st_uid=0)))

    def test_acl_for_other_user_or_group_is_rejected(self):
        from types import SimpleNamespace

        for user, group in ((os.geteuid() + 1, 0), (os.geteuid(), 4)):
            entries = [
                (1, 4, 0xFFFFFFFF),
                (2, 4, user),
                (4, group, 0xFFFFFFFF),
                (16, 4, 0xFFFFFFFF),
                (32, 0, 0xFFFFFFFF),
            ]
            with patch("os.getxattr", return_value=self.acl(entries)):
                self.assertFalse(credential_access(1, SimpleNamespace(st_mode=0o100440, st_uid=0)))

    def test_missing_malformed_or_extra_acl_entries_rejected(self):
        from types import SimpleNamespace

        meta = SimpleNamespace(st_mode=0o100440, st_uid=0)
        with patch("os.getxattr", side_effect=OSError()):
            self.assertFalse(credential_access(1, meta))
        for value in (b"", struct.pack("<I", 1) + b"x" * 40, b"x" * 52):
            with patch("os.getxattr", return_value=value):
                self.assertFalse(credential_access(1, meta))

    def test_real_cli_append_then_sigterm_removes_socket(self):
        directory, _path = self.creds()
        cfg = replace(
            Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"),
            data_root=self.root / "data",
        )
        repo = Repository(cfg)
        job, _ = repo.enqueue("service", "owner", "weather", "NORMAL", "input", repo.clock() + 3600)
        owner = repo.claim()
        ledger = Ledger(repo, self.key)
        env = ledger.begin(
            job["id"],
            owner["lease_token"],
            owner["attempt"],
            "fact_source",
            "RUNNING",
            "service_1",
            "a" * 64,
        )
        descriptor = generation(
            {
                "type": "segment",
                "app": cfg.app,
                "gen": env["body"]["gen"],
                "parent": None,
                "base_seq": 0,
                "base_digest": "0" * 64,
            },
            self.key,
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "snap_bot.n1_writer_service",
                "--enable-service",
                "--app",
                cfg.app,
                "--journal-root",
                str(self.root / "journal"),
                "--socket",
                str(self.path),
                "--worker-uid",
                str(os.getuid()),
            ],
            env=dict(os.environ, CREDENTIALS_DIRECTORY=str(directory)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready, _, _ = select.select([process.stdout], [], [], 3)
            self.assertTrue(ready)
            self.assertEqual(json.loads(process.stdout.readline())["status"], "READY")
            receipt = self.call({"generation": descriptor, "envelope": env})
            ledger.confirm(job["id"], owner["lease_token"], owner["attempt"], receipt)
            self.assertEqual(ledger.view(job["id"]), [receipt])
            process.send_signal(signal.SIGTERM)
            out, err = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, err)
            self.assertFalse(self.path.exists())
            self.assertNotIn(self.key.decode(), out + err)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
