import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from snap_bot import n1_ipc as ipc
from snap_bot.n1_contracts import Refused
from snap_bot.n1_segments import JournalSegments
from studio_core.config import Config
from studio_core.jobs import LeaseLost, Repository


@unittest.skipUnless(os.sys.platform == "linux", "Linux SO_PEERCRED integration")
class IPC(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "journal.sock"
        self.app = "snap-bot-v2"
        self.errors = []
        self.seen = []
        self.threads = []

    def listener(self, handler=None, worker_uid=None, raw=None):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        server.listen(4)
        server.settimeout(2)
        self.addCleanup(server.close)

        def serve():
            try:
                client, _ = server.accept()
                if raw:
                    with client:
                        raw(client)
                else:
                    ipc.serve_connection(
                        client,
                        worker_uid=os.getuid() if worker_uid is None else worker_uid,
                        app=self.app,
                        handler=handler or (lambda body: self.seen.append(body) or body),
                        timeout=0.3,
                    )
            except Exception as error:
                self.errors.append(error)

        thread = threading.Thread(target=serve)
        thread.start()
        self.threads.append(thread)
        return thread

    def tearDown(self):
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())

    def invoke(self, **kwargs):
        return ipc.call(
            self.path,
            server_uid=os.getuid(),
            app=self.app,
            request_id="request_1",
            envelope={"test": "synthetic"},
            timeout=0.8,
            **kwargs,
        )

    def raw(self, wire):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(self.path))
            client.sendall(wire)

    def test_roundtrip_uses_real_peer_credentials(self):
        self.listener()
        self.assertEqual(self.invoke(), {"test": "synthetic"})
        self.assertEqual(self.seen, [{"test": "synthetic"}])

    def test_unauthorized_client_never_invokes_handler(self):
        thread = self.listener(worker_uid=os.getuid() + 1)
        with self.assertRaises(ipc.RpcUncertain):
            self.invoke()
        thread.join(2)
        self.assertIsInstance(self.errors[0], ipc.PeerDenied)
        self.assertEqual(self.seen, [])

    def test_impostor_server_receives_no_envelope_bytes(self):
        observed = []

        def raw(client):
            client.settimeout(1)
            observed.append(client.recv(1024))

        self.listener(raw=raw)
        error_type = None
        try:
            ipc.call(
                self.path,
                server_uid=os.getuid() + 1,
                app=self.app,
                request_id="request_1",
                envelope={"private": "do-not-send"},
            )
        except Exception as error:
            error_type = type(error)
        self.assertIs(error_type, ipc.PeerDenied)
        self.threads[0].join(2)
        self.assertEqual(observed, [b""])

    def test_oversized_header_rejected_before_body_or_handler(self):
        self.listener()
        self.raw(struct.pack("!I", ipc.MAX_FRAME + 1))
        self.threads[0].join(2)
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(str(self.errors[0]), "FRAME_TOO_LARGE")
        self.assertEqual(self.seen, [])

    def test_duplicate_json_keys_rejected(self):
        self.listener()
        wire = b'{"v":1,"v":1,"app":"snap-bot-v2","request_id":"r","op":"append","body":{}}'
        self.raw(struct.pack("!I", len(wire)) + wire)
        self.threads[0].join(2)
        self.assertIsInstance(self.errors[0], ipc.ProtocolError)
        self.assertEqual(self.seen, [])

    def test_application_confusion_rejected_before_handler(self):
        self.listener()
        with self.assertRaises(ipc.RpcUncertain):
            ipc.call(
                self.path, server_uid=os.getuid(), app="status-bot-v2", request_id="r", envelope={}
            )
        self.assertEqual(self.seen, [])

    def test_partial_frame_eof_rejected(self):
        self.listener()
        self.raw(struct.pack("!I", 100) + b"{}")
        self.threads[0].join(2)
        self.assertIsInstance(self.errors[0], ipc.ProtocolError)

    def test_slow_header_hits_total_deadline(self):
        thread = self.listener()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(self.path))
            client.sendall(b"\0")
            started = time.monotonic()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 1)
        self.assertIsInstance(self.errors[0], TimeoutError)
        self.assertEqual(self.seen, [])

    def test_sensitive_handler_error_is_sanitized(self):
        def handler(body):
            raise ValueError("synthetic-private-token")

        self.listener(handler=handler)
        with self.assertRaises(ipc.RpcRefused) as error:
            self.invoke()
        self.assertEqual(str(error.exception), "APPEND_REFUSED")

    def test_lost_reply_is_unknown_and_not_retried(self):
        def raw(client):
            value = ipc.receive(client, ipc.budget(0.5))
            self.seen.append(value)
            # A durable append could have happened here; close without any reply.

        self.listener(raw=raw)
        with self.assertRaises(ipc.RpcUncertain):
            self.invoke()
        self.assertEqual(len(self.seen), 1)

    def test_response_request_binding_is_checked(self):
        def raw(client):
            ipc.receive(client, ipc.budget(0.5))
            ipc.send(
                client,
                {"v": 1, "app": self.app, "request_id": "other", "ok": True, "result": {}},
                ipc.budget(0.5),
            )

        self.listener(raw=raw)
        with self.assertRaises(ipc.RpcUncertain):
            self.invoke()

    def test_nonfinite_values_and_invalid_uids_refused(self):
        with self.assertRaises(ipc.ProtocolError):
            ipc.encode({"value": float("nan")})
        for invalid in (True, -1, 0x100000000, "0"):
            with self.assertRaises(ValueError):
                ipc.uid(invalid)

    def test_real_segment_append_over_socket_then_owner_confirmation(self):
        cfg = replace(
            Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"),
            data_root=self.root / "data",
        )
        self.app = cfg.app
        repo = Repository(cfg)
        job, _ = repo.enqueue("ipc", "owner", "weather", "NORMAL", "input", repo.clock() + 3600)
        owner = repo.claim()
        journal = JournalSegments(
            repo, b"synthetic-ipc-key-00000000000000000", self.root / "events"
        )
        envelope = journal.begin(
            job["id"],
            owner["lease_token"],
            owner["attempt"],
            "fact_source",
            "RUNNING",
            "event_1",
            "a" * 64,
        )
        self.listener(handler=ipc.SegmentAppendHandler(journal))
        receipt = ipc.call(
            self.path, server_uid=os.getuid(), app=cfg.app, request_id="append_1", envelope=envelope
        )
        self.assertEqual(journal.accepted(job["id"]), [])
        journal.confirm(job["id"], owner["lease_token"], owner["attempt"], receipt)
        self.assertEqual(journal.accepted(job["id"]), [receipt])
        self.assertEqual(journal.scan(envelope["body"]["gen"]), [receipt])

    def test_wrong_application_envelope_cannot_append(self):
        cfg = replace(
            Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"),
            data_root=self.root / "data",
        )
        journal = JournalSegments(
            Repository(cfg), b"synthetic-ipc-key-00000000000000000", self.root / "events"
        )
        handler = ipc.SegmentAppendHandler(journal)
        with self.assertRaises(ipc.ProtocolError):
            handler({"body": {"app": "other"}, "mac": "x"})

    def segment_fixture(self):
        cfg = replace(
            Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"),
            data_root=self.root / "data",
        )
        self.app = cfg.app
        repo = Repository(cfg)
        job, _ = repo.enqueue(
            "ipc-race", "owner", "weather", "NORMAL", "input", repo.clock() + 3600
        )
        owner = repo.claim()
        journal = JournalSegments(
            repo, b"synthetic-ipc-key-00000000000000000", self.root / "events"
        )
        envelope = journal.begin(
            job["id"],
            owner["lease_token"],
            owner["attempt"],
            "fact_source",
            "RUNNING",
            "race_1",
            "a" * 64,
        )
        return repo, job, owner, journal, envelope

    def test_lost_reply_after_real_fsync_preserves_unconfirmed_evidence(self):
        _repo, job, _owner, journal, envelope = self.segment_fixture()

        def raw(client):
            value = ipc.receive(client, ipc.budget(0.5))
            self.seen.append(journal.append(value["body"]))
            # Deliberate close after actual fsync but before returning the receipt.

        self.listener(raw=raw)
        with self.assertRaises(ipc.RpcUncertain):
            ipc.call(
                self.path,
                server_uid=os.getuid(),
                app=self.app,
                request_id="lost_1",
                envelope=envelope,
            )
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(journal.scan(envelope["body"]["gen"]), self.seen)
        self.assertEqual(journal.accepted(job["id"]), [])

    def test_live_old_writer_race_over_ipc_cannot_adopt_after_seal(self):
        repo, job, owner, journal, envelope = self.segment_fixture()
        ready = threading.Event()
        resume = threading.Event()
        received = []
        errors = []

        def pause():
            ready.set()
            if not resume.wait(3):
                raise TimeoutError("test barrier")

        self.listener(handler=lambda body: journal.append(body, before_write=pause))

        def client():
            try:
                received.append(
                    ipc.call(
                        self.path,
                        server_uid=os.getuid(),
                        app=self.app,
                        request_id="race_1",
                        envelope=envelope,
                        timeout=2,
                    )
                )
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=client)
        thread.start()
        self.threads.append(thread)
        try:
            self.assertTrue(ready.wait(2))
            with repo.transaction() as db:
                db.execute(
                    "UPDATE jobs SET lease_until=? WHERE id=?", (repo.clock() - 1, job["id"])
                )
            fresh = repo.claim(job_id=job["id"])
            self.assertEqual(fresh["attempt"], 2)
            journal.seal(envelope["body"]["gen"])
        finally:
            resume.set()
            thread.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(len(received), 1)
        with self.assertRaises((LeaseLost, Refused)):
            journal.confirm(job["id"], owner["lease_token"], owner["attempt"], received[0])
        self.assertEqual(journal.accepted(job["id"]), [])
        self.assertEqual(journal.scan(envelope["body"]["gen"]), [])

    def test_sealed_envelope_is_refused_over_ipc(self):
        _repo, job, _owner, journal, envelope = self.segment_fixture()
        journal.seal(envelope["body"]["gen"])
        self.listener(handler=ipc.SegmentAppendHandler(journal))
        with self.assertRaises(ipc.RpcRefused):
            ipc.call(
                self.path,
                server_uid=os.getuid(),
                app=self.app,
                request_id="sealed_1",
                envelope=envelope,
            )
        self.assertEqual(journal.accepted(job["id"]), [])


if __name__ == "__main__":
    unittest.main()
