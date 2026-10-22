import copy
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import chdir
from datetime import UTC
from pathlib import Path
from unittest.mock import patch

from snap_bot import actions_preview as actions

ROOT = Path(__file__).parents[3]


def environment():
    return {
        "GITHUB_REPOSITORY": actions.REPOSITORY,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_ACTOR": "Khalidshar",
        "GITHUB_TRIGGERING_ACTOR": "Khalidshar",
        "HUMAN_APPROVAL": "HUMAN_FRAME",
        "VIDEO_SHA": "a" * 64,
        "CONTENT_HASH": "b" * 64,
        "SOURCE_RUN": "7",
        "GITHUB_RUN_ID": "8",
        "GITHUB_SHA": "c" * 40,
        "SEND_INTENT_ARTIFACT_ID": "9",
    }


def blocked(call):
    try:
        call()
    except ValueError:
        return True
    return False


class ActionsPreviewTests(unittest.TestCase):
    def test_new_output_directory_creates_missing_parent(self):
        for name in ("manual-preview", "send-intent"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                output = Path(d) / "outputs" / name
                self.assertFalse(output.parent.exists())
                try:
                    actions.new_output_directory(output)
                except OSError as exc:
                    self.fail("guard:output-parent " + str(exc))
                self.assertTrue(output.is_dir(), "guard:output-parent")
                self.assertEqual(output.stat().st_mode & 0o777, 0o750, "guard:output-mode")

    def test_new_output_directory_rejects_existing_leaf(self):
        for name in ("manual-preview", "send-intent"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                output = Path(d) / "outputs" / name
                output.mkdir(parents=True)
                sentinel = output / "old.txt"
                sentinel.write_text("old output")
                with self.assertRaises(FileExistsError, msg="guard:output-no-reuse"):
                    actions.new_output_directory(output)
                self.assertEqual(sentinel.read_text(), "old output")

    def test_intent_preparation_creates_missing_parent_without_send(self):
        env = {
            "VIDEO_SHA": "a" * 64,
            "CONTENT_HASH": "b" * 64,
            "GITHUB_RUN_ID": "8",
            "GITHUB_ACTOR": "synthetic",
        }
        with (
            tempfile.TemporaryDirectory() as d,
            chdir(d),
            patch.dict(os.environ, env),
            patch.object(sys, "argv", ["actions_preview", "intent"]),
            patch.object(actions, "verify_bundle") as verify,
            patch.object(actions, "send_bundle") as send,
        ):
            self.assertFalse(Path("outputs").exists())
            try:
                actions.main()
            except OSError as exc:
                self.fail("guard:intent-output-parent " + str(exc))
            verify.assert_called_once()
            send.assert_not_called()
            output = Path("outputs/send-intent")
            self.assertEqual(output.stat().st_mode & 0o777, 0o750, "guard:intent-output-mode")
            self.assertEqual(
                json.loads((output / "intent.json").read_text())["state"], "EXTERNAL_EFFECT_INTENT"
            )
            original = (output / "intent.json").read_bytes()
            with self.assertRaises(FileExistsError, msg="guard:intent-output-no-reuse"):
                actions.main()
            self.assertEqual((output / "intent.json").read_bytes(), original)
            send.assert_not_called()

    def test_authority_and_explicit_approval_are_required(self):
        env = environment()
        actions.approval_guard(env)
        for key, value in [
            ("GITHUB_REPOSITORY", "other/repo"),
            ("GITHUB_EVENT_NAME", "push"),
            ("GITHUB_REF", "refs/heads/feature"),
            ("GITHUB_ACTOR", "other"),
            ("GITHUB_TRIGGERING_ACTOR", "other"),
            ("HUMAN_APPROVAL", ""),
            ("VIDEO_SHA", ""),
            ("CONTENT_HASH", ""),
            ("SOURCE_RUN", "bad"),
        ]:
            other = {**env, key: value}
            self.assertTrue(
                blocked(lambda: actions.approval_guard(other)), "guard:authority-" + key
            )

    def assert_preflight_format_blocked(self, key, invalid):
        for value in invalid:
            with (
                self.subTest(key=key, value=value),
                patch.object(actions, "github_get", return_value={}) as fetch,
            ):
                env = {**environment(), key: value}
                marker = "guard:preflight-format-" + key
                self.assertTrue(blocked(lambda: actions.preflight(env)), marker)
                self.assertFalse(fetch.called, marker)

    def test_video_sha_format_rejected_before_github_access(self):
        self.assert_preflight_format_blocked(
            "VIDEO_SHA",
            ["a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n", " " + "a" * 64, ""],
        )

    def test_content_hash_format_rejected_before_github_access(self):
        self.assert_preflight_format_blocked(
            "CONTENT_HASH",
            ["a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n", " " + "a" * 64, ""],
        )

    def test_source_run_format_rejected_before_github_access(self):
        self.assert_preflight_format_blocked(
            "SOURCE_RUN",
            [
                "0",
                "01",
                "0" * 20,
                "1" * 21,
                "",
                "-1",
                "+1",
                "1.0",
                " 1",
                "1\n",
                "١",
                "1;echo invalid",
            ],
        )

    def test_source_run_format_accepts_positive_canonical_ascii_digits(self):
        for value in ("1", "7", "10", "1" + "0" * 19, "9" * 20):
            with self.subTest(value=value):
                actions.approval_guard({**environment(), "SOURCE_RUN": value})

    def test_source_is_manual_successful_same_commit_recent(self):
        from datetime import datetime, timezone

        env = environment()
        run = {
            "id": 7,
            "event": "workflow_dispatch",
            "path": actions.RENDER_WORKFLOW,
            "conclusion": "success",
            "head_sha": env["GITHUB_SHA"],
            "repository": {"full_name": actions.REPOSITORY},
            "created_at": datetime.fromtimestamp(1000, UTC).isoformat(),
        }
        actions.source_guard(run, env, 1100)
        for key, value in [
            ("event", "push"),
            ("path", ".github/workflows/foundation.yml"),
            ("conclusion", "failure"),
            ("head_sha", "d" * 40),
            ("id", 6),
        ]:
            changed = {**run, key: value}
            self.assertTrue(
                blocked(lambda: actions.source_guard(changed, env, 1100)), "guard:source-" + key
            )
        self.assertTrue(
            blocked(lambda: actions.source_guard(run, env, 4600)), "guard:source-expiry"
        )

    def test_existing_intent_blocks_even_unknown_or_expired(self):
        env = environment()
        for expired in (False, True):
            self.assertTrue(
                blocked(
                    lambda: actions.history_guard(
                        [
                            {
                                "name": "did-you-know-send-attempt-" + env["VIDEO_SHA"],
                                "expired": expired,
                            }
                        ],
                        env["VIDEO_SHA"],
                    )
                ),
                "guard:no-repeat-intent",
            )
        actions.history_guard([], env["VIDEO_SHA"])

    def test_persistent_intent_matches_current_run_and_video(self):
        env = environment()
        artifact = {
            "id": 9,
            "name": "did-you-know-send-attempt-" + env["VIDEO_SHA"],
            "expired": False,
            "workflow_run": {"id": 8},
        }
        actions.intent_guard(env, artifact)
        for changed in (
            {**artifact, "id": 10},
            {**artifact, "name": "other"},
            {**artifact, "expired": True},
            {**artifact, "workflow_run": {"id": 7}},
        ):
            self.assertTrue(
                blocked(lambda: actions.intent_guard(env, changed)), "guard:durable-intent"
            )

    def test_actual_video_and_content_bindings(self):
        import hashlib

        video = b"reviewed exact bytes"
        env = environment()
        env["VIDEO_SHA"] = hashlib.sha256(video).hexdigest()
        result = {"video_sha256": env["VIDEO_SHA"], "content_hash": env["CONTENT_HASH"]}
        content = {"content_hash": env["CONTENT_HASH"]}
        actions.digest_guard(video, result, content, env)
        for key in ("VIDEO_SHA", "CONTENT_HASH"):
            altered = {**env, key: "0" * 64}
            self.assertTrue(
                blocked(lambda: actions.digest_guard(video, result, content, altered)),
                "guard:exact-" + key,
            )
        self.assertTrue(
            blocked(lambda: actions.digest_guard(video + b"changed", result, content, env)),
            "guard:actual-video-bytes",
        )

    def test_invalid_approval_never_reaches_github_or_telegram(self):
        env = environment()
        env["HUMAN_APPROVAL"] = ""
        with patch.object(actions, "github_get") as fetch:
            with self.assertRaises(ValueError):
                actions.send_bundle(Path("missing"), env)
            fetch.assert_not_called()

    # The lab's GitHub Actions workflows are not part of v3 (GRADUATION.md: actions path not moved).


@unittest.skipUnless(
    os.name == "posix" and os.environ.get("MADAR_ACTIONS_MUTATION") != "1",
    "Linux actual sandbox Actions acceptance",
)
class ActionsRealPreviewTests(unittest.TestCase):
    def test_owned_real_sandbox_preview_has_no_send_or_review_and_exact_hash_gates(self):
        with tempfile.TemporaryDirectory() as d, chdir(d):
            directory = Path("outputs/manual-preview").resolve()
            env = environment()
            self.assertFalse(Path("outputs").exists())
            started = time.monotonic()
            data = actions.render_bundle(
                directory, {"run_id": env["SOURCE_RUN"], "head_sha": env["GITHUB_SHA"]}
            )
            env["VIDEO_SHA"] = data["render"]["video_sha256"]
            env["CONTENT_HASH"] = data["render"]["content_hash"]
            config, repo, value, result = actions.verify_bundle(directory, env)
            self.assertEqual(value["delivery"], "NOT_ATTEMPTED")
            self.assertIsNone(value["human_approval"])
            self.assertTrue(
                all(result["network_isolation"].values()), "guard:real-render-isolation"
            )
            self.assertTrue((directory / "preview.png").is_file())
            self.assertTrue((directory / "did-you-know.mp4").is_file())
            with repo.connect() as db:
                self.assertFalse(
                    db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='human_frame_reviews'"
                    ).fetchone()
                )
                self.assertEqual(db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
            for key in ("VIDEO_SHA", "CONTENT_HASH"):
                changed = {**env, key: "0" * 64}
                self.assertTrue(
                    blocked(lambda: actions.verify_bundle(directory, changed)),
                    "guard:bundle-" + key,
                )
            path = directory / "preview.json"
            original = path.read_text(encoding="utf-8")
            changed = json.loads(original)
            changed["expires_at"] = time.time() - 1
            path.write_text(json.dumps(changed))
            self.assertTrue(
                blocked(lambda: actions.verify_bundle(directory, env)), "guard:bundle-expiry"
            )
            print(
                "Actual Actions owned preview seconds=" + str(round(time.monotonic() - started, 3))
            )
