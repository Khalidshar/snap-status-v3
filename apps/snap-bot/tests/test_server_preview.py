"""Behavioral server fences. Telegram transport is always fake."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_telegram_delivery as fixtures

from snap_bot import server_preview as server


@unittest.skipUnless(os.name == "posix", "Linux locking and fsync")
class ServerPreviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DeliveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = server.Runtime(Path(self.temp.name))
        self.commit = "a" * 40
        self.data = {
            "repository": "Khalidshar/snap-bot-v2",
            "job_id": self.fixture.job,
            "attempt": 1,
            "lease_token": None,
            "run_id": None,
            "head_sha": self.commit,
            "render": {"video_sha256": self.fixture.digest, "content_hash": "b" * 64},
        }

        def render(path, provenance):
            path.mkdir()
            (path / "did-you-know.mp4").write_bytes(self.fixture.video)
            self.data["run_id"] = provenance["run_id"]
            (path / "preview.json").write_text(json.dumps(self.data))
            return self.data

        replacements = [
            patch.object(server, "source_info", return_value=(self.commit, 1000)),
            patch.object(server, "owner_actor", return_value=1000),
            patch.object(server, "render_bundle", side_effect=render),
            patch.object(
                server,
                "verify_quality",
                side_effect=lambda *a: (self.fixture.config, self.fixture.repo, self.data, {}),
            ),
            patch("snap_bot.final_agent.record_human_frame", return_value={}),
            patch(
                "snap_bot.final_agent.evaluate_final",
                return_value={"delivery_decision": "APPROVED"},
            ),
        ]
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.evidence = self.runtime.render()
        self.sha = self.evidence["video_sha"]
        self.directory = self.runtime.root / "previews" / self.evidence["preview_id"]

    def approve(self):
        self.runtime.approve(self.sha)

    def test_no_approval(self):
        blocked = False
        try:
            self.runtime.send(self.sha, self.fixture.transport)
        except ValueError:
            blocked = True
        self.assertTrue(blocked and not self.fixture.calls, "guard:approval")

    def test_changed_sha(self):
        self.approve()
        (self.directory / "did-you-know.mp4").write_bytes(b"changed")
        blocked = False
        try:
            self.runtime.send(self.sha, self.fixture.transport)
        except ValueError:
            blocked = True
        self.assertTrue(blocked and not self.fixture.calls, "guard:sha")

    def test_intent_fsynced_before_network(self):
        self.approve()
        real_fsync = os.fsync
        synced = []
        checks = []

        def sync(fd):
            synced.append(os.readlink("/proc/self/fd/" + str(fd)))
            return real_fsync(fd)

        def transport(*args):
            marker = self.runtime.root / "intents" / (self.sha + ".json")
            checks.append(
                marker.exists()
                and str(marker) in synced
                and str(marker.parent) in synced
                and any(e["kind"] == "INTENT" for e in self.runtime.events())
                and str(self.runtime.root / "events.jsonl") in synced
                and str(self.runtime.root) in synced
            )
            return self.fixture.transport(*args)

        with patch.object(os, "fsync", side_effect=sync):
            self.runtime.send(self.sha, transport)
        self.assertTrue(checks and all(checks), "guard:intent-fsync")

    def test_unknown_permanently_blocks(self):
        self.approve()

        def transport(*args):
            if args[1] == "sendVideo":
                self.fixture.calls.append("sendVideo")
                raise TimeoutError()
            return self.fixture.transport(*args)

        with self.assertRaises(RuntimeError):
            self.runtime.send(self.sha, transport)
        self.assertEqual(self.runtime.events()[-1]["kind"], "UNKNOWN", "guard:unknown")
        before = list(self.fixture.calls)
        with self.assertRaises(ValueError):
            server.Runtime(self.runtime.root).send(self.sha, self.fixture.transport)
        self.assertEqual(before, self.fixture.calls, "guard:no-retry")

    def test_duplicate_rejected(self):
        self.approve()
        self.runtime.send(self.sha, self.fixture.transport)
        blocked = False
        try:
            server.Runtime(self.runtime.root).send(self.sha, self.fixture.transport)
        except ValueError:
            blocked = True
        self.assertTrue(blocked and self.fixture.calls.count("sendVideo") == 1, "guard:duplicate")

    def test_pre_network_failure_consumes_intent(self):
        self.approve()
        with patch.object(server, "deliver", side_effect=ValueError()):
            with self.assertRaises(RuntimeError):
                self.runtime.send(self.sha, self.fixture.transport)
        self.assertEqual(self.runtime.events()[-1]["kind"], "PRE_NETWORK_BLOCKED")
        with self.assertRaises(ValueError):
            self.runtime.send(self.sha, self.fixture.transport)
        self.assertEqual(self.fixture.calls, [])

    def test_wrong_bot_and_private_chat(self):
        self.approve()

        def transport(*args):
            return {"username": "other_bot"}

        with self.assertRaises(RuntimeError):
            self.runtime.send(self.sha, transport)
        self.assertEqual(self.runtime.events()[-1]["kind"], "UNKNOWN")

    def test_non_private_chat(self):
        self.approve()

        def transport(*args):
            if args[1] == "getChat":
                return {"id": 42, "type": "group"}
            if args[1] == "sendVideo":
                self.fail("Unexpected send")
            return self.fixture.transport(*args)

        with self.assertRaises(RuntimeError):
            self.runtime.send(self.sha, transport)
        self.assertNotIn("sendVideo", self.fixture.calls)

    def test_corrupt_ledger(self):
        path = self.runtime.root / "events.jsonl"
        value = json.loads(path.read_text())
        value["time"] += 1
        path.write_text(json.dumps(value) + "\n")
        blocked = False
        try:
            self.runtime.events()
        except ValueError:
            blocked = True
        self.assertTrue(blocked, "guard:ledger-hash")

    def test_partial_tail(self):
        path = self.runtime.root / "events.jsonl"
        path.write_bytes(path.read_bytes()[:-1])
        with self.assertRaises(ValueError):
            self.runtime.events()

    def test_journal_and_marker_independent_fences(self):
        self.approve()
        self.runtime.send(self.sha, self.fixture.transport)
        (self.runtime.root / "intents" / (self.sha + ".json")).unlink()
        with self.assertRaises(ValueError):
            self.runtime.send(self.sha, self.fixture.transport)
        server.exclusive_json(self.runtime.root / "intents" / (self.sha + ".json"), {})
        (self.runtime.root / "events.jsonl").unlink()
        with self.assertRaises(ValueError):
            self.runtime.send(self.sha, self.fixture.transport)

    def test_append_only_approval(self):
        before = (self.runtime.root / "events.jsonl").read_bytes()
        self.approve()
        self.assertTrue((self.runtime.root / "events.jsonl").read_bytes().startswith(before))
        with self.assertRaises(ValueError):
            self.approve()

    def pin_main(self, bot="rawwag_main_bot"):
        pins = {"test": server.EXPECTED_BOT, "main": bot}
        active = patch.object(server, "channel_bot", side_effect=lambda channel: pins[channel])
        active.start()
        self.addCleanup(active.stop)
        return pins

    def main_transport(self, token, method, *args):
        self.fixture.calls.append(method)
        if method == "getMe":
            return {"username": "rawwag_main_bot"}
        if method == "getChat":
            return {"id": 77, "type": "private"}
        return {"message_id": 9, "chat": {"id": 77}}

    def test_owner_chat_approves_the_main_channel_only(self):
        with patch.object(server.os, "geteuid", return_value=0), self.assertRaises(ValueError):
            self.runtime.approve(self.sha, "test", owner_chat=True)
        with patch.object(server.os, "geteuid", return_value=1000), self.assertRaises(ValueError):
            self.runtime.approve(self.sha, "main", owner_chat=True)
        self.assertNotIn("HUMAN_FRAME", [e["kind"] for e in self.runtime.events()])

    def test_approval_names_one_channel(self):
        self.pin_main()
        approval = self.runtime.approve(self.sha, channel="main")
        self.assertEqual((approval["channel"], approval["bot"]), ("main", "rawwag_main_bot"))
        with self.assertRaises(ValueError):
            self.runtime.send(self.sha, self.fixture.transport)
        self.assertEqual(self.fixture.calls, [], "guard:approval-channel")

    def test_test_approval_never_reaches_main(self):
        self.pin_main()
        self.approve()
        with self.assertRaises(ValueError):
            self.runtime.send(self.sha, self.main_transport, channel="main")
        self.assertEqual(self.fixture.calls, [], "guard:approval-channel")

    def test_main_pin_changed_after_approval(self):
        pins = self.pin_main()
        self.runtime.approve(self.sha, channel="main")
        pins["main"] = "another_main_bot"
        with self.assertRaises(ValueError):
            self.runtime.send(self.sha, self.main_transport, channel="main")
        self.assertEqual(self.fixture.calls, [], "guard:approval-bot")

    def test_main_send_acknowledged_with_channel(self):
        self.pin_main()
        self.runtime.approve(self.sha, channel="main")
        with patch.dict(os.environ, {"TG_TOKEN": "654321:" + ("y" * 35), "TG_CHAT": "77"}):
            ack = self.runtime.send(self.sha, self.main_transport, channel="main")
        self.assertEqual(
            (ack["kind"], ack["channel"], ack["bot"]), ("ACK", "main", "rawwag_main_bot")
        )
        self.assertEqual(self.fixture.calls.count("sendVideo"), 1)

    def test_main_channel_requires_installed_pin(self):
        with patch.object(server, "ROOT", self.runtime.root):
            with self.assertRaises(ValueError):
                server.channel_bot("main")
        self.assertEqual(server.channel_bot("test"), server.EXPECTED_BOT)
        with self.assertRaises(ValueError):
            server.channel_bot("staging")

    def test_concurrent_sends(self):
        from concurrent.futures import ThreadPoolExecutor

        self.approve()

        def send():
            try:
                return server.Runtime(self.runtime.root).send(self.sha, self.fixture.transport)[
                    "kind"
                ]
            except ValueError:
                return "BLOCKED"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: send(), range(2)))
        self.assertEqual(sorted(outcomes), ["ACK", "BLOCKED"])
        self.assertEqual(self.fixture.calls.count("sendVideo"), 1)

    def test_crash_after_intent_before_journal(self):
        self.approve()
        with patch.object(self.runtime, "append", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.runtime.send(self.sha, self.fixture.transport)
        self.assertEqual(self.fixture.calls, [])
        with self.assertRaises(ValueError):
            server.Runtime(self.runtime.root).send(self.sha, self.fixture.transport)

    def test_symlink_video(self):
        self.approve()
        video = self.directory / "did-you-know.mp4"
        original = self.directory / "original.mp4"
        video.rename(original)
        video.symlink_to(original)
        with self.assertRaises(ValueError):
            self.runtime.send(self.sha, self.fixture.transport)
        self.assertEqual(self.fixture.calls, [])

    def test_changed_evidence(self):
        self.approve()
        (self.directory / "evidence.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.runtime.send(self.sha, self.fixture.transport)
        self.assertEqual(self.fixture.calls, [])


@unittest.skipUnless(os.name == "posix", "Linux locking and fsync")
class FactHistoryTests(unittest.TestCase):
    """The ledger tells each fresh render which facts the owner has already seen."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = server.Runtime(Path(self.temp.name))
        (self.runtime.root / "previews").mkdir(parents=True)

    def rendered(self, n, fact_id, category, expires_at):
        preview, job, video = f"{n:032x}", f"{n + 100:032x}", f"{n:064x}"
        directory = self.runtime.root / "previews" / preview
        content = directory / "state/renders" / job / "input"
        content.mkdir(parents=True)
        (directory / "preview.json").write_text(json.dumps({"job_id": job}))
        (content / "content.json").write_text(
            json.dumps(
                {
                    "type": "did_you_know",
                    "fact_id": fact_id,
                    "category": category,
                    "body": "نص " + fact_id,
                    "expires_at": expires_at,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.runtime.append("RENDERED", video_sha=video, preview_id=preview)
        return video

    def test_sent_pending_and_released_renders(self):
        sent = self.rendered(1, "dyk-01", "animals", 1.0)
        self.runtime.append("ACK", video_sha=sent, message_id=1, bot="b", channel="main")
        unknown = self.rendered(2, "dyk-02", "body", 1.0)
        self.runtime.append("UNKNOWN", video_sha=unknown)
        stopped = self.rendered(3, "dyk-03", "space", 1.0)
        self.runtime.append("PRE_NETWORK_BLOCKED", video_sha=stopped)
        self.rendered(4, "dyk-04", "geography", 4_000_000_000.0)
        history = {h["fact_id"]: h for h in self.runtime.fact_history()}
        self.assertEqual(
            set(history), {"dyk-01", "dyk-02", "dyk-04"}, "guard:stopped-send-releases"
        )
        for fact in ("dyk-01", "dyk-02"):
            row = history[fact]
            self.assertAlmostEqual(row["until"] - row["at"], server.WINDOW_SECONDS)
        self.assertEqual(history["dyk-04"]["until"], 4_000_000_000.0)

    def test_missing_or_foreign_previews_are_skipped(self):
        self.runtime.append("RENDERED", video_sha="e" * 64, preview_id="f" * 32)
        self.runtime.append("RENDERED", video_sha="d" * 64, preview_id="../escape")
        self.assertEqual(self.runtime.fact_history(), [])

    def test_render_passes_history_to_the_renderer(self):
        sent = self.rendered(5, "dyk-05", "technology", 1.0)
        self.runtime.append("ACK", video_sha=sent, message_id=2, bot="b", channel="main")
        seen = {}

        def render(path, provenance):
            seen.update(provenance)
            raise RuntimeError("stop after provenance")

        with (
            patch.object(server, "source_info", return_value=("a" * 40, 1000)),
            patch.object(server, "render_bundle", side_effect=render),
            self.assertRaises(RuntimeError),
        ):
            self.runtime.render()
        self.assertEqual([h["fact_id"] for h in seen["fact_history"]], ["dyk-05"])


class OwnerAuthorityTests(unittest.TestCase):
    def test_owner_uid_required(self):
        for euid, raw in ((1000, "1000"), (0, "1001"), (0, ""), (0, "01000")):
            with (
                self.subTest(euid=euid, raw=raw),
                patch.object(os, "geteuid", return_value=euid),
                patch.dict(os.environ, {"SUDO_UID": raw}),
            ):
                blocked = False
                try:
                    server.owner_actor(1000)
                except ValueError:
                    blocked = True
                self.assertTrue(blocked, "guard:owner")
        with (
            patch.object(os, "geteuid", return_value=0),
            patch.dict(os.environ, {"SUDO_UID": "1000"}),
        ):
            self.assertEqual(server.owner_actor(1000), 1000)


@unittest.skipUnless(
    os.name == "posix" and os.environ.get("MADAR_ACTIONS_MUTATION") != "1",
    "Actual Linux render integration",
)
class ServerRealPreviewTests(unittest.TestCase):
    def test_root_controller_delegates_actual_render(self):
        import subprocess

        code = """
import json, os, pwd, tempfile
from pathlib import Path
from unittest.mock import patch
from snap_bot import server_preview as server
commit = os.environ['PROBE_COMMIT']
uid = int(os.environ['PROBE_OWNER_UID'])
assert os.geteuid() == 0
with tempfile.TemporaryDirectory(prefix='madar-root-render-probe-') as temporary:
    base = Path(temporary)
    base.chmod(0o755)
    work = base / 'render-work'
    work.mkdir(mode=0o711)
    with patch.object(server, 'RENDER_WORK', work), patch.object(server, 'source_info', return_value=(commit, uid)):
        runtime = server.Runtime(base / 'private-state')
        proof = runtime.render()
        assert proof['commit'] == commit
        runtime.approve(proof['video_sha'])
        calls = []
        def transport(token, method, body=b'', kind='application/json'):
            calls.append(method)
            if method == 'getMe': return {'username':'khalid_snap_v2_test_bot'}
            if method == 'getChat': return {'id':42,'type':'private'}
            return {'message_id':101,'chat':{'id':42}}
        with patch.dict(os.environ, {'TG_TEST_TOKEN':'123456:'+'x'*35,'TG_TEST_CHAT':'42'}):
            ack = runtime.send(proof['video_sha'], transport)
        assert ack['kind'] == 'ACK' and calls == ['getMe','getChat','sendVideo']
print('ROOT_CONTROLLER_NONROOT_RENDER_FAKE_SEND_PASS')
"""
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=server.ROOT, text=True
        ).strip()
        result = subprocess.run(
            [
                "sudo",
                "-n",
                "env",
                "PYTHONPATH=" + str(server.ROOT / "apps/snap-bot"),
                "PYTHONDONTWRITEBYTECODE=1",
                "PROBE_COMMIT=" + commit,
                "PROBE_OWNER_UID=" + str(os.getuid()),
                "/usr/bin/setpriv",
                "--no-new-privs",
                "--bounding-set=-all,+chown,+dac_override,+setuid,+setgid",
                server.sys.executable,
                "-c",
                code,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ROOT_CONTROLLER_NONROOT_RENDER_FAKE_SEND_PASS", result.stdout)

    def test_actual_render_approval_and_fake_delivery(self):

        commit = server.subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=server.ROOT, text=True
        ).strip()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(server, "source_info", return_value=(commit, os.getuid())),
            patch.object(server, "owner_actor", return_value=os.getuid()),
        ):
            runtime = server.Runtime(Path(directory))
            proof = runtime.render()
            self.assertEqual(proof["commit"], commit)
            runtime.approve(proof["video_sha"])
            calls = []

            def transport(token, method, body=b"", kind="application/json"):
                calls.append(method)
                if method == "getMe":
                    return {"username": "khalid_snap_v2_test_bot"}
                if method == "getChat":
                    return {"id": 42, "type": "private"}
                return {"message_id": 100, "chat": {"id": 42}}

            with patch.dict(
                os.environ, {"TG_TEST_TOKEN": "123456:" + "x" * 35, "TG_TEST_CHAT": "42"}
            ):
                ack = runtime.send(proof["video_sha"], transport)
            self.assertEqual(ack["kind"], "ACK")
            self.assertEqual(calls, ["getMe", "getChat", "sendVideo"])
            with self.assertRaises(ValueError):
                runtime.send(proof["video_sha"], transport)
