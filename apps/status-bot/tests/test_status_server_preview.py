"""Status server preview controller. Units, egress and render are always faked here;
the real run is the CI Release install job (live weather, no-network render)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from status_bot import server_preview as server
from studio_core.config import Config


def blocked(call, *args):
    try:
        call(*args)
    except ValueError:
        return True
    return False


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.run_dir = Path(temp.name)

    def test_run_config_points_only_into_the_run(self):
        path = server.run_config(self.run_dir)
        config = Config.load(path)
        self.assertEqual(config.data_root, (self.run_dir / "data").resolve())
        self.assertEqual(config.proxy_socket, (self.run_dir / "egress.sock").resolve())
        self.assertEqual(config.app, "status-bot-v2")
        self.assertEqual(config.network_hosts, ("api.open-meteo.com", "api.met.no"))

    def test_profile_drift_refused(self):
        original = Path.read_text
        with patch.object(
            Path, "read_text", lambda path, **k: original(path, **k).replace("egress.sock", "x")
        ):
            self.assertTrue(blocked(server.run_config, self.run_dir), "guard:pinned-profile")

    def test_settings_keep_pinned_fonts_and_tools(self):
        path = server.run_config(self.run_dir)
        with patch.object(
            server.shutil, "which", side_effect=lambda tool, path: "/usr/bin/" + tool
        ):
            settings = server.settings_for(path, self.run_dir)
        profile = settings["profile"]
        self.assertEqual(profile["fc_query"], "/usr/bin/fc-query")
        self.assertEqual(len(profile["font_sha256"]), 64)
        self.assertEqual(settings["egress_socket"], str(self.run_dir / "egress.sock"))
        with patch.object(server.shutil, "which", return_value=None):
            self.assertTrue(blocked(server.settings_for, path, self.run_dir), "guard:tools")
        with patch.object(server.shutil, "which", return_value="/usr/local/bin/ffmpeg"):
            self.assertTrue(blocked(server.settings_for, path, self.run_dir), "guard:pinned-tool")

    def test_state_root(self):
        self.assertEqual(server.state_root(None), server.STATE)
        self.assertEqual(server.state_root("/var/lib/x/status"), Path("/var/lib/x/status"))
        for value in ("relative", "/tmp/status", "/var/lib/../tmp/status"):  # noqa: S108  # rejected
            with self.subTest(value=value):
                self.assertTrue(blocked(server.state_root, value), "guard:state-root")


@unittest.skipUnless(os.name == "posix", "Linux locking and fsync")
class LedgerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.runtime = server.Runtime(Path(temp.name) / "status")

    def test_chain_and_tamper(self):
        with self.runtime.locked():
            self.runtime.append("RENDERED", video_sha="a" * 64)
            self.runtime.append("RENDERED", video_sha="b" * 64)
        self.assertEqual([e["seq"] for e in self.runtime.events()], [1, 2])
        path = self.runtime.root / "events.jsonl"
        lines = path.read_text().splitlines()
        path.write_text(lines[0].replace("a" * 64, "c" * 64) + "\n" + lines[1] + "\n")
        self.assertTrue(blocked(self.runtime.events), "guard:ledger-chain")
        path.write_text(lines[0])
        self.assertTrue(blocked(self.runtime.events), "guard:partial-tail")

    def test_symlink_state_refused(self):
        target = self.runtime.root.parent / "elsewhere"
        target.mkdir()
        link = self.runtime.root.parent / "link"
        link.symlink_to(target)
        self.assertTrue(blocked(server.Runtime, link / "status"), "guard:symlink-state")


@unittest.skipUnless(os.name == "posix", "Linux locking and fsync")
class RenderFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.runtime = server.Runtime(Path(temp.name) / "status")
        self.calls = []
        self.video = b"video-bytes"
        import hashlib

        self.sha = hashlib.sha256(self.video).hexdigest()

    def fake_prepare(self, cfg, settings, client, secret, event_id, *, synthetic):
        self.calls.append(("prepare", synthetic, type(client).__name__))
        self.cfg = cfg
        job = "f" * 32
        output = cfg.data_root / "renders" / job / "output"
        output.mkdir(parents=True)
        (output / "weather.mp4").write_bytes(self.video)
        (output / "preview.png").write_bytes(b"png")
        (output / "render-result.json").write_text("{}")
        content = {"content_hash": "d" * 64, "source": "open-meteo", "expires_at": 9e9}
        return {"claim": {"id": job}, "content": content}

    def fake_render(self, unit, run, payload):
        self.calls.append(("render", json.loads(payload.read_text())["job"]))
        return {
            "job_id": "f" * 32,
            "video_sha256": self.sha,
            "content_hash": "d" * 64,
            "qa": {"result": "PASS"},
            "network_isolation": {"ipv4": True},
        }

    def render(self, **extra):
        patches = [
            patch.object(server, "release_commit", return_value="e" * 40),
            patch.object(server, "prepare", side_effect=self.fake_prepare),
            patch.object(server, "record_validation", return_value={"manifest": True}),
            patch.object(server.Runtime, "start_egress", lambda *a: self.calls.append(("egress",))),
            patch.object(server.Runtime, "render_unit", lambda s, *a: self.fake_render(*a)),
            patch.object(server, "systemctl", lambda *a: self.calls.append(("systemctl", *a))),
            patch.object(
                server, "settings_for", return_value={"profile": {}, "egress_socket": "x"}
            ),
        ]
        for name, value in extra.items():
            patches.append(patch.object(server, name, value))
        for active in patches:
            active.start()
            self.addCleanup(active.stop)
        return self.runtime.render()

    def test_live_source_preview_and_ledger(self):
        value = self.render()
        self.assertEqual(self.calls[0], ("egress",))
        self.assertEqual(self.calls[1], ("prepare", False, "NetworkClient"), "guard:live-source")
        self.assertEqual(self.calls[2][0], "render")
        stopped = [c[2] for c in self.calls if c[:2] == ("systemctl", "stop")]
        self.assertEqual(len(stopped), 2, "guard:transient-units-stopped")
        self.assertEqual(value["delivery"], "NOT_ATTEMPTED")
        self.assertEqual(value["video_sha"], self.sha)
        preview = self.runtime.root / "previews" / value["preview_id"]
        self.assertEqual(
            sorted(p.name for p in preview.iterdir()),
            ["evidence.json", "manifest.json", "preview.png", "render-result.json", "weather.mp4"],
        )
        (event,) = self.runtime.events()
        self.assertEqual((event["kind"], event["video_sha"]), ("RENDERED", self.sha))

    def test_units_stopped_when_render_fails(self):
        def failing(*args):
            raise RuntimeError("render unit failed")

        with self.assertRaises(RuntimeError):
            self.render(record_validation=failing)
        stopped = [c[2] for c in self.calls if c[:2] == ("systemctl", "stop")]
        self.assertEqual(len(stopped), 2, "guard:cleanup-on-failure")
        self.assertEqual(self.runtime.events(), [], "guard:no-ledger-on-failure")

    def test_changed_video_refused(self):
        def changed(cfg, prepared, result):
            path = cfg.data_root / "renders" / prepared["claim"]["id"] / "output/weather.mp4"
            path.write_bytes(b"swapped")
            return {}

        self.assertTrue(blocked(lambda: self.render(record_validation=changed)), "guard:sha")
        self.assertEqual(self.runtime.events(), [])


class CommandTests(unittest.TestCase):
    def test_render_unit_is_isolated(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            line = json.dumps({"job_id": "f" * 32})
            return SimpleNamespace(returncode=0, stdout="noise\n" + line + "\n", stderr="")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(server.subprocess, "run", fake_run),
        ):
            run = Path(directory)
            result = server.Runtime(run / "state").render_unit("u.service", run, run / "p.json")
        command = captured["command"]
        self.assertEqual(result, {"job_id": "f" * 32})
        for flag in (
            "--property=PrivateNetwork=yes",
            "--property=RestrictAddressFamilies=AF_UNIX",
            "--property=CapabilityBoundingSet=",
            "--property=InaccessiblePaths=" + str(run / "egress.sock"),
        ):
            self.assertIn(flag, command, "guard:render-isolation")

    def test_egress_unit_scope(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            return SimpleNamespace(returncode=0)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(server.subprocess, "run", fake_run),
            patch.object(Path, "is_socket", return_value=True),
        ):
            run = Path(directory)
            server.Runtime(run / "state").start_egress("e.service", run / "c.toml", run / "s.sock")
        command = captured["command"]
        self.assertIn("--property=ReadWritePaths=" + str(run), command)
        self.assertIn("--property=CapabilityBoundingSet=", command)
        self.assertEqual(command[-3:], ["--config", str(run / "c.toml"), "proxy"])

    def test_main_requires_root(self):
        with (
            patch.object(server.os, "geteuid", return_value=1000),
            patch.object(server.sys, "argv", ["x", "render"]),
            self.assertRaises(SystemExit),
        ):
            server.main()


if __name__ == "__main__":
    unittest.main()
