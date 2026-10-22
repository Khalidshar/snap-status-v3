import copy
import json
import multiprocessing
import os
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from n1_spawn_targets import crash_after_durable

from snap_bot.n1_generations import GenerationAuthority, RegisteredWriter
from snap_bot.n1_writer import PhysicalWriter, WriterRefused, generation, mac


@unittest.skipUnless(os.name == "posix", "Linux generation authority")
class Generations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = b"generation-authority-synthetic-key-00000"
        self.physical = PhysicalWriter(self.root / "journal", self.key, "snap-bot-v2")
        self.writer = RegisteredWriter(self.physical)
        self.authority = self.writer.authority
        self.desc = self.describe("a" * 32)

    def describe(self, gen, parent=None, seq=0, digest="0" * 64):
        return generation(
            {
                "type": "segment",
                "app": "snap-bot-v2",
                "gen": gen,
                "parent": parent,
                "base_seq": seq,
                "base_digest": digest,
            },
            self.key,
        )

    def envelope(self, desc, intent="b" * 32, seq=None, prev=None):
        header = desc["body"]
        body = {
            "app": "snap-bot-v2",
            "job_id": "fixture",
            "attempt": 1,
            "agent": "fact_source",
            "status": "RUNNING",
            "event_id": "fixture",
            "intent_id": intent,
            "owner_fp": "c" * 64,
            "payload_sha256": "d" * 64,
            "not_after": time.time() + 60,
            "kid": "p0-fixture-v1",
            "gen": header["gen"],
            "seq": header["base_seq"] + 1 if seq is None else seq,
            "prev": header["base_digest"] if prev is None else prev,
        }
        return {"body": body, "mac": mac(self.key, "n1.intent.v1", body)}

    def test_signed_but_unregistered_generation_cannot_append(self):
        with self.assertRaisesRegex(WriterRefused, "GENERATION_UNREGISTERED"):
            self.writer.append(self.desc, self.envelope(self.desc))
        self.assertFalse(self.physical.path(self.desc["body"]).exists())

    def test_registration_survives_new_writer_instance(self):
        self.authority.register(self.desc)
        fresh = RegisteredWriter(PhysicalWriter(self.physical.root, self.key, "snap-bot-v2"))
        self.assertEqual(
            fresh.append(self.desc, self.envelope(self.desc))["envelope"]["body"]["gen"], "a" * 32
        )

    def test_sealed_generation_rejects_even_same_duplicate_intent(self):
        self.authority.register(self.desc)
        env = self.envelope(self.desc)
        self.writer.append(self.desc, env)
        self.authority.seal("a" * 32, self.describe("e" * 32, parent="a" * 32))
        with self.assertRaisesRegex(WriterRefused, "GENERATION_SEALED"):
            self.writer.append(self.desc, env)
        self.assertEqual(self.authority.register(self.desc), "SEALED")

    def test_child_admission_uses_confirmed_prefix_not_orphan_tip(self):
        self.authority.register(self.desc)
        receipt = self.writer.append(self.desc, self.envelope(self.desc))
        second = self.envelope(self.desc, intent="e" * 32, seq=2, prev=receipt["digest"])
        self.writer.append(self.desc, second)
        before = self.physical.path(self.desc["body"]).read_bytes()
        child = self.describe("f" * 32, parent="a" * 32, seq=1, digest=receipt["digest"])
        self.authority.seal("a" * 32, child)
        fresh = self.writer.append(child, self.envelope(child))
        self.assertEqual(fresh["envelope"]["body"]["seq"], 2)
        self.assertEqual(self.physical.path(self.desc["body"]).read_bytes(), before)

    def test_bad_anchor_refused_without_sealing_parent(self):
        self.authority.register(self.desc)
        with self.assertRaisesRegex(WriterRefused, "SEAL_ANCHOR"):
            self.authority.seal(
                "a" * 32, self.describe("e" * 32, parent="a" * 32, seq=1, digest="f" * 64)
            )
        self.assertEqual(self.authority.load()["generations"]["a" * 32]["state"], "ACTIVE")

    def test_idempotent_seal_and_conflicting_second_child(self):
        self.authority.register(self.desc)
        child = self.describe("e" * 32, parent="a" * 32)
        self.authority.seal("a" * 32, child)
        self.assertEqual(self.authority.seal("a" * 32, child), "SEALED")
        with self.assertRaisesRegex(WriterRefused, "SEAL_CONFLICT"):
            self.authority.seal("a" * 32, self.describe("f" * 32, parent="a" * 32))

    def test_child_cannot_register_without_authority_seal(self):
        with self.assertRaisesRegex(WriterRefused, "REGISTER_ROOT_ONLY"):
            self.authority.register(self.describe("e" * 32, parent="a" * 32))

    def test_registry_descriptor_conflict_cannot_redirect_append(self):
        self.authority.register(self.desc)
        altered = self.describe("a" * 32, parent="e" * 32)
        with self.assertRaisesRegex(WriterRefused, "GENERATION_REGISTERED_CONFLICT"):
            self.writer.append(altered, self.envelope(altered))

    def test_corrupt_registry_is_not_reset(self):
        self.authority.register(self.desc)
        self.authority.path.write_bytes(b"{partial")
        with self.assertRaisesRegex(WriterRefused, "REGISTRY_CORRUPT"):
            self.authority.register(self.describe("e" * 32))
        self.assertEqual(self.authority.path.read_bytes(), b"{partial")

    def test_missing_registry_with_physical_evidence_fails_closed(self):
        self.authority.register(self.desc)
        self.writer.append(self.desc, self.envelope(self.desc))
        self.authority.path.unlink()
        with self.assertRaisesRegex(WriterRefused, "REGISTRY_MISSING_WITH_EVIDENCE"):
            self.authority.register(self.desc)

    def test_symlink_or_public_registry_refused(self):
        self.authority.register(self.desc)
        self.authority.path.chmod(0o644)
        with self.assertRaisesRegex(WriterRefused, "REGISTRY_FILE"):
            self.authority.load()
        self.authority.path.unlink()
        outside = self.root / "outside"
        outside.write_bytes(b"unchanged")
        self.authority.path.symlink_to(outside)
        with self.assertRaises(OSError):
            self.authority.load()
        self.assertEqual(outside.read_bytes(), b"unchanged")

    def test_write_fsync_failure_never_returns_registration(self):
        with patch("snap_bot.n1_generations.os.fsync", side_effect=OSError("synthetic fsync")):
            with self.assertRaises(OSError):
                self.authority.register(self.desc)
        self.assertFalse(self.authority.path.exists())

    def test_directory_fsync_failure_requires_explicit_retry(self):
        with patch("snap_bot.n1_generations.sync_dir", side_effect=OSError("synthetic dir fsync")):
            with self.assertRaises(OSError):
                self.authority.register(self.desc)
        with patch("snap_bot.n1_generations.sync_dir", side_effect=OSError("synthetic dir fsync")):
            with self.assertRaises(OSError):
                self.authority.register(self.desc)
        self.assertEqual(self.authority.register(self.desc), "ACTIVE")

    def test_seal_waits_for_admitted_append_then_blocks_late_write(self):
        self.authority.register(self.desc)
        gate = threading.Event()
        entered = threading.Event()
        sealed = threading.Event()
        errors = []
        actual = self.physical.append
        child = self.describe("e" * 32, parent="a" * 32)

        def blocked(desc, env):
            entered.set()
            gate.wait(3)
            return actual(desc, env)

        def append():
            try:
                self.writer.append(self.desc, self.envelope(self.desc))
            except Exception as error:
                errors.append(error)

        def seal():
            try:
                self.authority.seal("a" * 32, child)
                sealed.set()
            except Exception as error:
                errors.append(error)

        with patch.object(self.physical, "append", side_effect=blocked):
            first = threading.Thread(target=append)
            second = threading.Thread(target=seal)
            first.start()
            try:
                self.assertTrue(entered.wait(1))
                second.start()
                self.assertFalse(sealed.wait(0.15))
            finally:
                gate.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(sealed.is_set())
        with self.assertRaisesRegex(WriterRefused, "GENERATION_SEALED"):
            self.writer.append(self.desc, self.envelope(self.desc))

    def test_sigkill_after_fsync_leaves_one_line_and_explicit_retry_recovers_receipt(self):
        self.authority.register(self.desc)
        env = self.envelope(self.desc)
        process = multiprocessing.get_context("spawn").Process(
            target=crash_after_durable, args=(self.physical.root, self.key, self.desc, env)
        )
        process.start()
        try:
            process.join(5)
            self.assertEqual(process.exitcode, -signal.SIGKILL)
        finally:
            if process.is_alive():
                process.kill()
                process.join(3)
        path = self.physical.path(self.desc["body"])
        before = path.read_bytes()
        fresh = RegisteredWriter(PhysicalWriter(self.physical.root, self.key, "snap-bot-v2"))
        receipt = fresh.append(self.desc, env)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(fresh.writer.scan(path, self.desc["body"])), 1)
        self.assertEqual(receipt["envelope"], env)
        # This journal retry is not a bot job or delivery replay.

    def test_registry_capacity_is_bounded(self):
        self.authority.CAP = 1
        self.authority.register(self.desc)
        with self.assertRaisesRegex(WriterRefused, "REGISTRY_CAP"):
            self.authority.register(self.describe("e" * 32))


if __name__ == "__main__":
    unittest.main()
