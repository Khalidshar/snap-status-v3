"""Release installer and host check guards. Nothing here writes outside a temp dir."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from snap_bot import check_env
from snap_bot import release as installer

ROOT = Path(__file__).resolve().parents[3]
PINNED = "ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023 the FFmpeg developers"
SECRET = "/etc/studio-v2/snap-telegram-test.env"


def blocked(call, *args, **kwargs):
    try:
        call(*args, **kwargs)
    except ValueError:
        return True
    return False


def commit_tree(root: Path, files: dict[str, str]) -> str:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.org"]
    subprocess.run(["git", "-C", str(root), *identity, "commit", "-qm", "fixture"], check=True)
    return installer.git(root, "rev-parse", "HEAD").decode().strip()


def layout(snap_deps: str = '["studio-core"]') -> dict[str, str]:
    files = {name: "" for name in installer.REQUIRED}
    files["apps/snap-bot/pyproject.toml"] = "[project]\ndependencies = " + snap_deps + "\n"
    files["packages/studio-core/pyproject.toml"] = "[project]\ndependencies = []\n"
    files["apps/status-bot/pyproject.toml"] = '[project]\ndependencies = ["studio-core"]\n'
    return files


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = Path(temp.name)
        self.commit = commit_tree(self.source, layout())
        for active in (
            patch.object(installer.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=1000)),
            patch.object(installer, "telegram_file", side_effect=lambda path: Path(path)),
        ):
            active.start()
            self.addCleanup(active.stop)

    def plan(self, **kwargs):
        return installer.build_plan(self.source, self.commit, sys.executable, SECRET, **kwargs)

    def test_external_paths_and_legacy_units(self):
        for path in (
            "/opt/studio-v2/snap",
            "/tmp/install",  # noqa: S108  # rejected path, never created
            "/etc/studio-v2/snap-telegram-test.env",
            "/etc/systemd/system/studio-v2-snap.service",
            "/etc/systemd/system/madar-snap-render.service",
            "/etc/systemd/system/madar-other.service",
            "/opt/madar/../studio-v2/file",
            "relative",
        ):
            with self.subTest(path=path):
                self.assertTrue(blocked(installer.destination, path), "guard:allowed-paths")

    def test_symlink_parent(self):
        original = Path.is_symlink
        with patch.object(
            Path, "is_symlink", lambda path: path == Path("/opt/madar") or original(path)
        ):
            target = "/opt/madar/releases/" + self.commit
            self.assertTrue(blocked(installer.destination, target), "guard:symlink-path")

    def test_existing_release(self):
        original = Path.exists
        release = installer.RELEASES / self.commit
        with patch.object(Path, "exists", lambda path: path == release or original(path)):
            self.assertTrue(blocked(self.plan), "guard:immutable-release")

    def test_inexact_commit(self):
        self.commit = self.commit[:12]
        self.assertTrue(blocked(self.plan), "guard:exact-commit")

    def test_third_party_dependency_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            self.source = Path(directory)
            self.commit = commit_tree(self.source, layout('["studio-core", "requests"]'))
            self.assertTrue(blocked(self.plan), "guard:no-unlocked-dependencies")

    def test_incomplete_layout_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            files = layout()
            del files["apps/snap-bot/snap_bot/check_env.py"]
            self.source = Path(directory)
            self.commit = commit_tree(self.source, files)
            self.assertTrue(blocked(self.plan), "guard:release-layout")

    def test_root_owner_refused(self):
        with patch.object(installer.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=0)):
            self.assertTrue(blocked(self.plan), "guard:non-root-owner")

    def test_differing_unit_refused(self):
        original = Path.exists
        unit = installer.UNIT_DIR / installer.UNITS[0]
        with (
            patch.object(Path, "exists", lambda path: path == unit or original(path)),
            patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=0, st_mode=0o644)),
            patch.object(Path, "read_text", return_value="[Service]\nExecStart=/bin/sh\n"),
        ):
            self.assertTrue(blocked(self.plan), "guard:existing-unit")

    def test_plan_only_allowed_writes_and_never_activates(self):
        plan = self.plan()
        for path in plan["writes"]:
            installer.destination(path)
        self.assertNotIn(str(installer.BASE / "current"), plan["writes"], "guard:no-activation")
        self.assertIs(plan["activates"], False)
        self.assertEqual(plan["systemd_actions"], [])
        self.assertEqual(sorted(Path(x).name for x in plan["units"]), sorted(installer.UNITS))
        release = installer.RELEASES / self.commit
        self.assertEqual(
            plan["path_file"].splitlines(),
            [
                str(release / "apps/snap-bot"),
                str(release / "apps/status-bot"),
                str(release / "packages/studio-core"),
            ],
            "guard:release-import-roots",
        )

    def test_units_network_and_no_automatic_approval(self):
        render, send = installer.unit_text(), installer.unit_text(True, SECRET)
        self.assertIn("PrivateNetwork=yes", render)
        self.assertIn("RestrictAddressFamilies=AF_UNIX\n", render)
        self.assertNotIn("EnvironmentFile", render)
        self.assertIn("EnvironmentFile=" + SECRET + "\n", send)
        self.assertIn("--video-sha %i", send)
        for text in (render, send):
            self.assertIn("Type=oneshot", text)
            self.assertIn("ProtectSystem=strict", text)
            self.assertIn("-m snap_bot.server_preview", text)
            self.assertNotIn(" approve", text)
            self.assertNotIn("[Install]", text)
            self.assertNotIn("snap_app", text)
        self.assertTrue(blocked(installer.unit_text, True), "guard:send-needs-env-file")

    def test_unit_names_differ_from_lab(self):
        self.assertTrue(all(name.startswith("madar-v3-") for name in installer.UNITS))

    def test_status_unit_renders_only(self):
        text = installer.unit_for("madar-v3-status-render.service", SECRET)
        self.assertIn("-m status_bot.server_preview render\n", text)
        self.assertIn("PrivateNetwork=yes", text)
        self.assertIn("RestrictAddressFamilies=AF_UNIX\n", text)
        self.assertIn("ReadWritePaths=/var/lib/madar/status\n", text)
        for absent in ("EnvironmentFile", " send", "AF_INET", "[Install]"):
            self.assertNotIn(absent, text, "guard:status-no-delivery")
        status = [name for name in installer.UNITS if "status" in name]
        self.assertEqual(status, ["madar-v3-status-render.service"], "guard:status-render-only")

    def test_main_channel_is_opt_in(self):
        plan = self.plan()
        self.assertEqual(plan["channels"], {"test": {"bot": installer.TEST_BOT}})
        self.assertNotIn(installer.MAIN_UNIT, [Path(x).name for x in plan["units"]])
        self.assertNotIn(installer.COMMANDS_UNIT, [Path(x).name for x in plan["units"]])

    def test_main_channel_unit_and_pinned_bot(self):
        main = "/etc/madar/snap-telegram-main.env"
        plan = self.plan(main_env=main, main_bot_name="rawwag_main_bot")
        self.assertEqual(plan["channels"]["main"], {"bot": "rawwag_main_bot"})
        units = {Path(x).name: text for x, text in plan["units"].items()}
        text = units[installer.MAIN_UNIT]
        self.assertIn("EnvironmentFile=" + main + "\n", text)
        self.assertIn(" send --channel main --video-sha %i\n", text, "guard:main-unit-channel")
        self.assertNotIn(SECRET, text, "guard:main-unit-own-env")
        # The test unit is byte-for-byte what earlier releases installed.
        self.assertEqual(units["madar-v3-snap-send@.service"], installer.unit_text(True, SECRET))
        listener = units[installer.COMMANDS_UNIT]
        self.assertIn("EnvironmentFile=" + main + "\n", listener)
        self.assertIn("Type=simple\nRestart=on-failure\n", listener)
        self.assertIn("-m snap_bot.owner_commands\n", listener, "guard:commands-unit")
        for absent in (SECRET, " send ", " approve", "[Install]"):
            self.assertNotIn(absent, listener)
        for path in plan["writes"]:
            installer.destination(path)

    def test_whatsapp_send_unit_is_opt_in_and_networked_alone(self):
        plain = [Path(x).name for x in self.plan()["units"]]
        self.assertNotIn(installer.WHATSAPP_UNIT, plain, "guard:whatsapp-opt-in")
        env = "/etc/madar/status-whatsapp.env"
        units = {Path(x).name: t for x, t in self.plan(whatsapp_env=env)["units"].items()}
        text = units[installer.WHATSAPP_UNIT]
        self.assertIn("EnvironmentFile=" + env + "\n", text)
        self.assertIn(" -m status_bot.server_preview send --video-sha %i\n", text)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n", text)
        self.assertNotIn("PrivateNetwork=yes", text)
        self.assertNotIn(SECRET, text, "guard:whatsapp-own-env")
        # The render unit keeps no network, byte for byte.
        self.assertEqual(units["madar-v3-status-render.service"], installer.status_unit_text())
        self.assertIn("PrivateNetwork=yes", installer.status_unit_text())
        self.assertTrue(blocked(self.plan, whatsapp_env=SECRET), "guard:whatsapp-env-distinct")

    def test_kind_render_unit_renders_one_kind_without_network(self):
        units = {Path(x).name: t for x, t in self.plan()["units"].items()}
        text = units[installer.KIND_RENDER_UNIT]
        self.assertIn(" -m snap_bot.server_preview render --kind %i\n", text)
        self.assertIn("PrivateNetwork=yes", text, "guard:kind-render-no-network")
        self.assertNotIn("EnvironmentFile", text)
        self.assertEqual(units["madar-v3-snap-render.service"], installer.unit_text())

    def test_main_channel_refusals(self):
        main = "/etc/madar/snap-telegram-main.env"
        for kwargs in (
            {"main_env": main},
            {"main_bot_name": "rawwag_main_bot"},
            {"main_env": main, "main_bot_name": installer.TEST_BOT},
            {"main_env": main, "main_bot_name": "not a bot"},
            {"main_env": main, "main_bot_name": "rawwag_main"},
            {"main_env": SECRET, "main_bot_name": "rawwag_main_bot"},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertTrue(blocked(self.plan, **kwargs), "guard:main-channel")

    def test_source_commit_and_dirty_refused(self):
        self.assertTrue(blocked(installer.source_files, self.source, "a" * 40), "guard:commit")
        (self.source / installer.REQUIRED[0]).write_text("dirty")
        self.assertTrue(
            blocked(installer.source_files, self.source, self.commit), "guard:dirty-source"
        )

    def test_unsafe_source_symlink_refused(self):
        os.symlink("/etc/passwd", self.source / "link")
        subprocess.run(["git", "-C", str(self.source), "add", "link"], check=True)
        identity = ["-c", "user.name=Test", "-c", "user.email=test@example.org"]
        subprocess.run(["git", "-C", str(self.source), *identity, "commit", "-qm", "l"], check=True)
        commit = installer.git(self.source, "rev-parse", "HEAD").decode().strip()
        self.assertTrue(blocked(installer.source_files, self.source, commit), "guard:symlink")

    def test_execute_requires_root(self):
        with patch.object(installer.os, "geteuid", return_value=1000):
            self.assertTrue(blocked(installer.execute, self.plan()), "guard:root-installer")


class TelegramFileTests(unittest.TestCase):
    def test_location_and_trust(self):
        outside = ("relative.env", "/tmp/x.env", "/etc/../tmp/x.env")  # noqa: S108  # rejected
        for path in outside:
            with self.subTest(path=path):
                self.assertTrue(blocked(installer.telegram_file, path), "guard:env-location")
        self.assertTrue(blocked(installer.telegram_file, "/etc/madar-missing.env"))
        with (
            patch.object(Path, "is_symlink", return_value=False),
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=1000, st_mode=0o600)),
        ):
            self.assertTrue(blocked(installer.telegram_file, SECRET), "guard:env-owner")
        with (
            patch.object(Path, "is_symlink", return_value=False),
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=0, st_mode=0o600)),
        ):
            self.assertEqual(installer.telegram_file(SECRET), Path(SECRET))


class ActivationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name) / "madar"
        self.commit = "b" * 40
        self.release = base / "releases" / self.commit
        (self.release / ".venv/bin").mkdir(parents=True)
        (self.release / ".venv/bin/python").write_text("")
        self.manifest = self.release / "madar-release.json"
        for active in (
            patch.object(installer, "BASE", base),
            patch.object(installer, "RELEASES", base / "releases"),
            patch.object(installer, "destination", side_effect=self.local),
        ):
            active.start()
            self.addCleanup(active.stop)
        self.base = base
        self.uid = os.geteuid()

    def local(self, path, allow_current=False):
        return Path(path)

    def write(self, **value):
        self.manifest.write_text(json.dumps({"commit": self.commit, **value}))

    def activate(self, dry_run=False):
        real = Path.stat

        def as_root(path, **kwargs):
            stat = real(path, **kwargs)
            return SimpleNamespace(st_uid=0, st_mode=stat.st_mode)

        with patch.object(Path, "stat", as_root):
            return installer.activate(self.commit, dry_run)

    def test_lab_and_v3_releases_activate(self):
        self.write(environment="PASS")
        self.assertEqual(self.activate(True)["layout"], "lab", "guard:rollback-to-lab")
        self.write(environment="PASS", layout="v3")
        result = self.activate()
        self.assertEqual(result, {"activated": self.commit, "layout": "v3", "previous": None})
        self.assertEqual((self.base / "current").resolve(), self.release.resolve())

    def test_unverified_release_refused(self):
        self.write(environment="FAIL")
        self.assertTrue(blocked(self.activate), "guard:verified-release")
        self.manifest.unlink()
        self.assertTrue(blocked(self.activate), "guard:manifest-required")
        self.write(environment="PASS")
        (self.release / ".venv/bin/python").unlink()
        self.assertTrue(blocked(self.activate), "guard:venv-required")

    def test_untrusted_owner_refused(self):
        self.write(environment="PASS")
        self.assertTrue(blocked(installer.activate, "c" * 40), "guard:missing-release")
        if self.uid != 0:
            self.assertTrue(blocked(installer.activate, self.commit), "guard:root-owned")


class RuntimeEnvTests(unittest.TestCase):
    def setUp(self):
        self.pinned = check_env.profiles()

    def facts(self):
        return {
            "os": {"ID": "ubuntu", "VERSION_ID": "24.04", "PRETTY_NAME": "Ubuntu 24.04.3 LTS"},
            "python": (3, 12),
            "euid": 1001,
            "init": "systemd",
            "sudo": True,
            "ffmpeg": [PINNED],
            "tools": {"/usr/bin/ffprobe": True, "/usr/bin/fc-query": True},
            "systemd_run": True,
            "node": "v22.11.0",
        }

    def test_pinned_profiles_share_one_ffmpeg_build(self):
        self.assertEqual({p["ffmpeg_version"] for p in self.pinned}, {PINNED})

    def test_matching_host_passes(self):
        self.assertEqual(check_env.problems(self.facts(), self.pinned, require_node=True), [])

    def test_each_drift_is_reported_by_name(self):
        cases = {
            "os": ({"ID": "ubuntu", "VERSION_ID": "22.04"}, "Ubuntu 24.04"),
            "python": ((3, 11), "Python 3.12"),
            "ffmpeg": (["ffmpeg version 4.4.2"], "FFmpeg must be"),
            "init": ("init", "systemd must be PID 1"),
            "euid": (0, "non-root"),
            "sudo": (False, "passwordless sudo"),
            "systemd_run": (False, "missing /usr/bin/systemd-run"),
            "node": ("", "Node 22"),
        }
        for key, (value, message) in cases.items():
            with self.subTest(key=key):
                facts = self.facts()
                facts[key] = value
                found = check_env.problems(facts, self.pinned, require_node=True)
                self.assertTrue(any(message in item for item in found), "guard:drift-" + key)

    def test_missing_tool_and_isolation_opt_out(self):
        facts = self.facts()
        facts["tools"]["/usr/bin/fc-query"] = False
        facts.update(euid=0, sudo=False, init="init", node="", systemd_run=False)
        found = check_env.problems(facts, self.pinned, require_isolation=False)
        self.assertEqual(found, ["missing /usr/bin/fc-query"])

    def test_repository_fonts_are_hash_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets/fonts/cairo").mkdir(parents=True)
            (root / "assets/fonts/cairo/Cairo-Regular.ttf").write_bytes(b"tampered")
            profile = {
                "font_files": {
                    "cairo/Cairo-Regular.ttf": "0" * 64,
                    "kufi/Missing.ttf": "0" * 64,
                    "system:Amiri-Regular": "0" * 64,
                }
            }
            self.assertEqual(
                check_env.font_problems(profile, root),
                ["font hash mismatch cairo/Cairo-Regular.ttf", "missing font kufi/Missing.ttf"],
            )
        repo = [p for profile in self.pinned for p in check_env.font_problems(profile)]
        self.assertEqual(repo, [], "guard:repo-fonts-intact")

    def test_probe_and_cli_report(self):
        facts = check_env.probe()
        self.assertEqual(set(facts), set(self.facts()))
        self.assertEqual(check_env.os_release(Path("/nonexistent")), {})
        self.assertIsNone(check_env.run(["/nonexistent-tool"]))
        with patch.object(check_env, "probe", return_value=self.facts()):
            self.assertEqual(check_env.main(["--require-node"]), 0)
        with patch.object(check_env, "probe", return_value={**self.facts(), "node": ""}):
            self.assertEqual(check_env.main(["--require-node"]), 1)


if __name__ == "__main__":
    unittest.main()
