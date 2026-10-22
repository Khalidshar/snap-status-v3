"""Atomic publication and attempt-scoped orphan cleanup on Linux."""

import errno
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


@contextmanager
def disk_full():
    real_fdopen = os.fdopen

    class FullStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, data):
            self.stream.write(data[: len(data) // 2])
            self.stream.flush()
            raise OSError(errno.ENOSPC, "synthetic disk full during real temporary write")

    with patch(
        "snap_bot.render.os.fdopen", side_effect=lambda fd, mode: FullStream(real_fdopen(fd, mode))
    ):
        yield


@unittest.skipUnless(sys.platform == "linux", "Atomic attempt files require Linux")
class AtomicAttemptWrites(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "attempts/1"
        self.root.mkdir(parents=True)
        self.path = self.root / "content.json"

    def test_enospc_stage_input_can_retry_same_owner_attempt(self):
        from test_owned_contract_adapter import AdapterServiceTests

        from snap_bot.render_commit import stage_input

        a = AdapterServiceTests()
        a.setUp()
        self.addCleanup(a.doCleanups)
        f = a.f
        claim = a.claim
        with disk_full(), self.assertRaises(OSError):
            stage_input(f.settings, f.repo, f.job, claim["lease_token"], 1, f.content)
        target = Path(f.settings["input_root"]) / f.job / "attempts/1/input/content.json"
        self.assertFalse(target.exists(), msg="guard:enospc-same-attempt")
        stage_input(f.settings, f.repo, f.job, claim["lease_token"], 1, f.content)
        self.assertEqual(json.loads(target.read_bytes()), f.content)
        self.assertEqual(f.repo.assert_owner(f.job, claim["lease_token"], attempt=1)["attempt"], 1)

    def test_fsync_precedes_link_and_link_never_replaces_inode(self):
        from snap_bot.render import write_once

        events = []
        real_fsync = os.fsync
        real_link = os.link

        def fsync(fd):
            events.append("sync")
            return real_fsync(fd)

        def link(a, b):
            self.assertFalse(self.path.exists())
            self.assertIn("sync", events)
            events.append("link")
            return real_link(a, b)

        with (
            patch("snap_bot.render.os.fsync", side_effect=fsync),
            patch("snap_bot.render.os.link", side_effect=link),
        ):
            write_once(self.path, b"complete")
        self.assertEqual(events[-1], "sync")
        self.assertEqual(list(self.root.glob(".tmp-*")), [])
        inode = self.path.stat().st_ino
        write_once(self.path, b"complete")
        with self.assertRaisesRegex(ValueError, "immutable"):
            write_once(self.path, b"different")
        self.assertEqual((self.path.stat().st_ino, self.path.read_bytes()), (inode, b"complete"))

    def old(self, parent, name):
        p = parent / name
        p.write_bytes(b"orphan")
        os.utime(p, (time.time() - 7200,) * 2)
        return p

    def test_cleanup_keeps_final_fresh_other_attempt_and_symlink(self):
        from snap_bot.render import cleanup_attempt_temps

        orphan = self.old(self.root, ".tmp-" + "a" * 32)
        final = self.old(self.root, "content.json")
        unknown = self.old(self.root, ".tmp-not-ours")
        fresh = self.root / (".tmp-" + "b" * 32)
        fresh.write_bytes(b"active-age")
        other = self.root.parent / "2"
        other.mkdir()
        outside = self.old(other, ".tmp-" + "c" * 32)
        linked = self.root / (".tmp-" + "d" * 32)
        linked.symlink_to(outside)
        directory = self.root / (".tmp-" + "e" * 32)
        directory.mkdir()
        fifo = self.root / (".tmp-" + "f" * 32)
        os.mkfifo(fifo)
        shortcut = self.root / "elsewhere"
        shortcut.symlink_to(other, target_is_directory=True)
        cleanup_attempt_temps(self.root)
        self.assertFalse(orphan.exists(), msg="guard:orphan-cleanup")
        for p in (final, unknown, fresh, outside, linked, directory, fifo):
            self.assertTrue(p.exists(), msg="guard:cleanup-scope")

    def test_cleanup_preserves_old_locked_writer(self):
        import fcntl

        from snap_bot.render import cleanup_attempt_temps

        old = self.old(self.root, ".tmp-" + "a" * 32)
        with old.open("rb") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            cleanup_attempt_temps(self.root)
            self.assertTrue(old.exists(), msg="guard:active-temp-lock")
        cleanup_attempt_temps(self.root)
        self.assertFalse(old.exists())

    def test_attempt_start_cleans_own_old_temporary_only(self):
        from snap_bot.render import attempt_workspace

        root = self.root.parent.parent
        self.old(self.root, ".tmp-" + "a" * 32)
        attempt_workspace(root, 1)
        self.assertEqual(list(self.root.glob(".tmp-*")), [], msg="guard:startup-cleanup")
