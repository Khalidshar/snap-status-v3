import functools
import os
import socket
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from snap_bot import n1_ipc as ipc
from snap_bot.n1_contracts import Ledger, Refused
from snap_bot.n1_control import ControlClient, ControlHandler, ControlService, request, serve
from snap_bot.n1_coordinator import JournalCoordinator
from snap_bot.n1_generations import RegisteredWriter
from snap_bot.n1_writer import IsolatedAppendHandler, PhysicalWriter
from studio_core.config import Config
from studio_core.jobs import LeaseLost, Repository


@unittest.skipUnless(os.name == "posix", "Linux control integration")
class Control(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = b"control-integration-synthetic-key-000000"
        self.writer = RegisteredWriter(
            PhysicalWriter(self.root / "journal", self.key, "snap-bot-v2")
        )
        self.path = self.root / "control.sock"
        self.service = ControlService(
            self.path, os.getuid(), "snap-bot-v2", ControlHandler(self.writer.authority)
        )
        self.service.start()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.service.run, args=(self.stop,))
        self.thread.start()
        self.client = ControlClient(self.path, os.getuid(), "snap-bot-v2")
        cfg = replace(
            Config.load(Path(__file__).parents[3] / "config/snap/weather.toml"),
            data_root=self.root / "data",
        )
        self.repo = Repository(cfg)
        self.job, _ = self.repo.enqueue(
            "control", "owner", "weather", "NORMAL", "input", self.repo.clock() + 3600
        )
        self.owner = self.repo.claim()
        self.ledger = Ledger(self.repo, self.key)
        self.coordinator = JournalCoordinator(self.ledger, self.client)

    def tearDown(self):
        self.stop.set()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.service.shutdown(3)

    def args(self):
        return (self.job["id"], self.owner["lease_token"], self.owner["attempt"])

    def begin(self, event="control_1"):
        return self.coordinator.begin(*self.args(), "fact_source", "RUNNING", event, "a" * 64)

    def test_real_socket_control_then_append_and_db_adoption(self):
        body = self.begin()
        receipt = IsolatedAppendHandler(self.writer)(body)
        self.ledger.confirm(*self.args(), receipt)
        self.assertEqual(self.ledger.view(self.job["id"]), [receipt])

    def test_worker_append_transport_cannot_issue_control(self):
        with self.assertRaises(ipc.RpcUncertain):
            ipc.call(
                self.path,
                server_uid=os.getuid(),
                app="snap-bot-v2",
                request_id="wrong_op",
                envelope={"action": "register"},
                timeout=1,
            )
        self.assertFalse(self.writer.authority.path.exists())

    def test_control_does_not_expose_append_or_database_paths(self):
        # A reply can arrive before the connection thread releases its admission slot.
        # This test exercises two admitted application refusals, not busy backpressure.
        finished = threading.Event()
        original = self.service._serve

        @functools.wraps(original)
        def serve_completed(conn):
            try:
                original(conn)
            finally:
                finished.set()

        self.service._serve = serve_completed
        for body in (
            {"action": "append"},
            {"action": "register", "descriptor": {}, "db_path": "forbidden"},
        ):
            finished.clear()
            with self.assertRaises(ipc.RpcRefused):
                self.client.call(body, "forbidden")
            self.assertTrue(finished.wait(2), "CONTROL_REFUSAL_SLOT_RELEASE_NOT_OBSERVED")
        self.assertFalse(self.writer.authority.path.exists())

    def test_wrong_server_uid_rejected_before_request(self):
        wrong = ControlClient(self.path, os.getuid() + 1, "snap-bot-v2")
        try:
            wrong.call({"action": "register"}, "wrong_server")
        except ipc.PeerDenied:
            denied = True
        except Exception:
            denied = False
        else:
            denied = False
        self.assertTrue(denied)
        self.assertFalse(self.writer.authority.path.exists())

    def test_control_authenticates_before_receiving_any_frame(self):
        client, server = socket.socketpair()
        try:
            try:
                serve(server, os.getuid() + 1, "snap-bot-v2", lambda body: body, timeout=0.1)
            except ipc.PeerDenied:
                denied = True
            except Exception:
                denied = False
            else:
                denied = False
            self.assertTrue(denied)
        finally:
            client.close()
            server.close()

    def test_wrong_authority_uid_never_registers(self):
        self.service.worker_uid = os.getuid() + 1
        with self.assertRaises(ipc.RpcUncertain):
            self.client.call({"action": "register"}, "wrong_authority")
        self.assertFalse(self.writer.authority.path.exists())

    def test_wrong_app_and_operation_fail_closed(self):
        for app, op in (("status-bot-v2", "control"), ("snap-bot-v2", "append")):
            with self.assertRaises(ipc.ProtocolError):
                request(
                    {"v": 1, "app": app, "request_id": "x", "op": op, "body": {}}, "snap-bot-v2"
                )

    def test_rotate_preserves_confirmed_prefix_and_rejects_old_generation(self):
        first = self.begin()
        receipt = IsolatedAppendHandler(self.writer)(first)
        self.ledger.confirm(*self.args(), receipt)
        child = self.coordinator.rotate(*self.args())
        self.assertEqual(child["body"]["base_digest"], receipt["digest"])
        with self.assertRaisesRegex(Exception, "GENERATION_SEALED"):
            IsolatedAppendHandler(self.writer)(first)
        second = self.begin("control_2")
        next_receipt = IsolatedAppendHandler(self.writer)(second)
        self.ledger.confirm(*self.args(), next_receipt)
        self.assertEqual(len(self.ledger.view(self.job["id"])), 2)

    def test_physical_seal_failure_keeps_db_fenced_and_same_pending_child(self):
        self.begin()
        original = self.client.seal
        children = []

        def fail(parent, desc):
            children.append(desc["body"]["gen"])
            raise ipc.RpcUnavailable("synthetic")

        self.client.seal = fail
        with self.assertRaises(ipc.RpcUnavailable):
            self.coordinator.rotate(*self.args())
        with self.repo.connect() as db:
            old = db.execute("SELECT * FROM n1_segments WHERE state='SEALED'").fetchone()
            new = db.execute("SELECT * FROM n1_segments WHERE state='ACTIVE'").fetchone()
            self.assertIsNotNone(old)
            self.assertEqual(new["gen"], children[0])
            self.assertEqual(db.execute("SELECT state FROM n1_ledger").fetchone()[0], "UNKNOWN")
        self.client.seal = original
        fresh = self.coordinator.ensure_current(*self.args())
        self.assertEqual(fresh["body"]["gen"], children[0])
        self.assertEqual(self.writer.authority.load()["generations"][old["gen"]]["state"], "SEALED")

    def test_lost_seal_reply_retries_same_child_without_new_generation(self):
        # The retry is a new connection; with one admission slot it must not race the
        # first seal's slot release, or it is busy-rejected as CONTROL_OUTCOME_UNKNOWN.
        released = threading.Event()
        serve = self.service._serve

        @functools.wraps(serve)
        def serve_released(conn):
            try:
                serve(conn)
            finally:
                released.set()

        self.service._serve = serve_released
        self.begin()
        original = self.client.seal
        children = []

        def lost(parent, desc):
            children.append(desc["body"]["gen"])
            released.clear()
            original(parent, desc)
            raise ipc.RpcUncertain("synthetic lost ACK")

        self.client.seal = lost
        with self.assertRaises(ipc.RpcUncertain):
            self.coordinator.rotate(*self.args())
        self.assertTrue(released.wait(2), "CONTROL_SEAL_SLOT_RELEASE_NOT_OBSERVED")
        self.client.seal = original
        fresh = self.coordinator.ensure_current(*self.args())
        self.assertEqual(fresh["body"]["gen"], children[0])
        self.assertEqual(len(self.writer.authority.load()["generations"]), 2)

    def test_expired_owner_cannot_rotate_or_mark_control_confirmed(self):
        with self.repo.transaction() as db:
            db.execute(
                "UPDATE jobs SET lease_until=? WHERE id=?", (self.repo.clock() - 1, self.job["id"])
            )
        with self.assertRaises(LeaseLost):
            self.coordinator.rotate(*self.args())
        self.assertFalse(self.writer.authority.path.exists())

    def test_owner_loss_after_control_commit_cannot_confirm_db_ack(self):
        original = self.client.register

        def expire(desc):
            result = original(desc)
            with self.repo.transaction() as db:
                db.execute(
                    "UPDATE jobs SET lease_until=? WHERE id=?",
                    (self.repo.clock() - 1, self.job["id"]),
                )
            return result

        self.client.register = expire
        with self.assertRaises(LeaseLost):
            self.coordinator.ensure_current(*self.args())
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT confirmed FROM n1_control").fetchone()[0], 0)

    def test_wrong_control_ack_is_not_confirmed(self):
        self.client.register = lambda desc: {"gen": "0" * 32, "state": "ACTIVE"}
        with self.assertRaisesRegex(Refused, "CONTROL_ACK_BINDING"):
            self.coordinator.ensure_current(*self.args())
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT confirmed FROM n1_control").fetchone()[0], 0)

    def test_legacy_rotated_history_without_link_fails_closed(self):
        with self.repo.connect() as db:
            old = db.execute("SELECT gen FROM n1_segments WHERE state='ACTIVE'").fetchone()[0]
        self.ledger.seal(old)
        with self.assertRaisesRegex(Refused, "CONTROL_HISTORY_MISSING"):
            self.coordinator.ensure_current(*self.args())

    def test_network_call_occurs_after_owned_db_transaction_commits(self):
        original = self.client.register

        def checked(desc):
            with self.repo.transaction() as db:
                self.assertEqual(db.execute("SELECT confirmed FROM n1_control").fetchone()[0], 0)
            return original(desc)

        self.client.register = checked
        self.assertEqual(self.coordinator.ensure_current(*self.args())["body"]["parent"], None)


if __name__ == "__main__":
    unittest.main()
