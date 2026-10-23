import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from snap_bot.n1_journal_service import JournalService
from snap_bot.n1_writer import PhysicalWriter
from snap_bot.n1_writer_service import ServiceRefused


@unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "non-root Linux daemon lifecycle")
class JournalLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.writer = PhysicalWriter(
            self.root / "journal", b"dual-plane-lifecycle-synthetic-key-0000", "snap-bot-v2"
        )
        self.worker = os.getuid() + 10000
        self.authority = os.getuid() + 20000
        self.service = JournalService(
            self.root / "append.sock",
            self.root / "control.sock",
            self.worker,
            self.authority,
            self.writer,
            "snap-bot-v2",
        )

    def test_requires_three_distinct_nonroot_roles(self):
        for worker, authority in [
            (0, self.authority),
            (self.worker, 0),
            (self.worker, self.worker),
            (os.getuid(), self.authority),
            (self.worker, os.getuid()),
        ]:
            with self.assertRaisesRegex(ServiceRefused, "ROLE_SEPARATION"):
                JournalService(
                    self.root / "a", self.root / "c", worker, authority, self.writer, "snap-bot-v2"
                )

    def test_requires_distinct_absolute_paths(self):
        for a, c in [
            (self.root / "same", self.root / "same"),
            (Path("relative"), self.root / "control"),
        ]:
            with self.assertRaisesRegex(ServiceRefused, "SOCKET_SEPARATION"):
                JournalService(a, c, self.worker, self.authority, self.writer, "snap-bot-v2")

    def test_existing_control_path_not_removed_and_append_rolled_back(self):
        control = self.root / "control.sock"
        control.write_text("foreign")
        with self.assertRaisesRegex(ServiceRefused, "ALREADY_EXISTS"):
            self.service.start()
        self.assertFalse((self.root / "append.sock").exists())
        self.assertEqual(control.read_text(), "foreign")

    def test_graceful_stop_cleans_both_sockets(self):
        self.service.start()
        stop = threading.Event()
        thread = threading.Thread(target=self.service.run, args=(stop,))
        thread.start()
        stop.set()
        thread.join(3)
        self.service.shutdown(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse((self.root / "append.sock").exists())
        self.assertFalse((self.root / "control.sock").exists())

    def test_failure_of_either_listener_stops_other_plane(self):
        for which in ("append", "control"):
            service = JournalService(
                self.root / ("a" + which),
                self.root / ("c" + which),
                self.worker,
                self.authority,
                self.writer,
                "snap-bot-v2",
            )
            service.start()
            stop = threading.Event()
            with (
                patch.object(
                    getattr(service, which),
                    "run",
                    side_effect=OSError("synthetic secret must not appear"),
                ),
                self.assertRaisesRegex(ServiceRefused, "^JOURNAL_PLANE_FAILED$"),
            ):
                service.run(stop)
            self.assertTrue(stop.is_set())
            service.shutdown(3)

    def test_unexpected_clean_listener_exit_fails_supervisor(self):
        self.service.start()
        stop = threading.Event()
        with patch.object(self.service.control, "run", return_value=None):
            with self.assertRaisesRegex(ServiceRefused, "PLANE_FAILED"):
                self.service.run(stop)
        self.assertTrue(stop.is_set())
        self.service.shutdown(3)

    def test_readiness_requires_both_sockets_started(self):
        with self.assertRaisesRegex(ServiceRefused, "NOT_STARTED"):
            self.service.run(threading.Event())
        self.service.start()
        try:
            with self.assertRaisesRegex(ServiceRefused, "ALREADY_STARTED"):
                self.service.start()
            self.assertTrue((self.root / "append.sock").exists())
            self.assertTrue((self.root / "control.sock").exists())
            self.assertEqual(self.service.append.worker_uid, self.worker)
            self.assertEqual(self.service.control.worker_uid, self.authority)
        finally:
            self.service.shutdown(3)

    def test_created_control_socket_removed_when_startup_later_fails(self):
        original = self.service.control.start

        def failed():
            original()
            raise OSError("synthetic post-bind failure")

        with patch.object(self.service.control, "start", side_effect=failed):
            with self.assertRaises(OSError):
                self.service.start()
        self.assertFalse((self.root / "append.sock").exists())
        self.assertFalse((self.root / "control.sock").exists())

    def test_app_matches_physical_writer(self):
        with self.assertRaisesRegex(ServiceRefused, "APP_BINDING"):
            JournalService(
                self.root / "a",
                self.root / "c",
                self.worker,
                self.authority,
                self.writer,
                "status-bot-v2",
            )

    def test_uses_registered_writer_not_raw_physical_append(self):
        from snap_bot.n1_generations import RegisteredWriter

        self.assertIsInstance(self.service.append.handler.writer, RegisteredWriter)
        self.assertIs(
            self.service.control.handler.authority, self.service.append.handler.writer.authority
        )


if __name__ == "__main__":
    unittest.main()
