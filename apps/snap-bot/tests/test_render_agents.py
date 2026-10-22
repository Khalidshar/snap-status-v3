import json
import unittest
from pathlib import Path

import test_owned_contract_adapter as fixtures

from snap_bot.agent_contract_map import RENDER_CHECKS
from snap_bot.content_agents import AgentRejected
from snap_bot.owned_contract_adapter import ContractViolation, digest, require_delivery_contract
from snap_bot.render_agents import check_media, check_voice, judge, mp4_boxes, run_render_check


def changed(result, **qa):
    return {**result, "qa": {**result["qa"], **qa}}


class VoiceAgentTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.OwnedContractAdapterTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def audio(self, **values):
        return changed(self.f.result, audio_qa={**self.f.result["qa"]["audio_qa"], **values})

    def test_accepts_the_renderer_limits_at_their_edges(self):
        for values in (
            {"integrated_lufs": -19.0},
            {"integrated_lufs": -17.0},
            {"true_peak_dbtp": -1.5},
            {"phone_lufs": -24.0},
        ):
            with self.subTest(values=values):
                self.assertEqual(check_voice(self.audio(**values))["decision"], "PASS")

    def test_rejects_each_audio_problem(self):
        for result, code in (
            (
                changed(self.f.result, audio="not_required_for_weather_card"),
                "VOICE_AUDIO_NOT_AMBIENT",
            ),
            (changed(self.f.result, audio_qa=None), "VOICE_MEASUREMENT_MISSING"),
            (self.audio(integrated_lufs=float("nan")), "VOICE_MEASUREMENT_INVALID"),
            (self.audio(integrated_lufs=True), "VOICE_MEASUREMENT_INVALID"),
            (self.audio(integrated_lufs=-16.9), "VOICE_LOUDNESS_OUT_OF_RANGE"),
            (self.audio(integrated_lufs=-19.1), "VOICE_LOUDNESS_OUT_OF_RANGE"),
            (self.audio(true_peak_dbtp=-1.4), "VOICE_PEAK_TOO_HIGH"),
            (self.audio(phone_lufs=-24.1), "VOICE_TOO_QUIET_ON_PHONE"),
        ):
            with (
                self.subTest(code=code),
                self.assertRaisesRegex(AgentRejected, code, msg="guard:voice-" + code),
            ):
                check_voice(result)


class MediaAgentTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.OwnedContractAdapterTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.settings = self.f.artifacts()
        self.video = Path(self.settings["spool_root"]).parent / self.f.result["artifact"]

    def reject(self, code, result=None, guard=None):
        with self.assertRaisesRegex(AgentRejected, code, msg="guard:" + (guard or code)):
            check_media(result or self.f.result, self.settings)

    def test_accepts_the_declared_faststart_video(self):
        self.assertEqual(
            check_media(self.f.result, self.settings)["boxes"], ["ftyp", "moov", "mdat"]
        )

    def test_rejects_bytes_that_are_not_the_declared_digest(self):
        self.video.write_bytes(fixtures.faststart_mp4(b"other synthetic video"))
        self.reject("MEDIA_DIGEST_MISMATCH")

    def test_rejects_a_symlinked_video(self):
        real = self.video.with_name("real.mp4")
        self.video.rename(real)
        self.video.symlink_to(real)
        self.reject("MEDIA_PATH_UNSAFE")

    def test_rejects_size_and_profile_claims_that_do_not_match(self):
        self.reject(
            "MEDIA_SIZE_MISMATCH", changed(self.f.result, bytes=len(self.f.video) + 1), "media-size"
        )
        self.settings["profile"] = {**self.settings["profile"], "max_bytes": len(self.f.video) - 1}
        self.reject("MEDIA_SIZE_MISMATCH", guard="media-max-bytes")
        self.settings["profile"] = fixtures.PROFILE
        for qa in ({"width": 720}, {"height": 1280}, {"codec": "hevc"}, {"duration": 8.3}):
            with self.subTest(qa=qa):
                self.reject("MEDIA_PROFILE_MISMATCH", changed(self.f.result, **qa), "media-profile")

    def test_rejects_a_video_whose_index_follows_its_data(self):
        box = fixtures.box
        data = box(b"ftyp", b"isom") + box(b"mdat", b"x") + box(b"moov", b"y")
        self.assertEqual(mp4_boxes(data), ["ftyp", "mdat", "moov"])
        result = self.rewrite(data)
        self.reject("MEDIA_NOT_FASTSTART", result, "media-faststart")

    def test_rejects_malformed_or_incomplete_containers(self):
        box = fixtures.box
        overrun = box(b"ftyp", b"isom") + box(b"moov", b"y") + b"\x00\x00\x01\x00mdat" + b"x"
        for data in (
            overrun,
            b"not an mp4 file",
            box(b"ftyp", b"isom") + b"\x00\x00\x00\x40mdat",
            box(b"moov") + box(b"mdat"),
            box(b"ftyp") + box(b"mdat", b"x"),
        ):
            with self.subTest(data=data):
                self.reject("MEDIA_CONTAINER_INVALID", self.rewrite(data), "media-container")

    def test_rejects_a_result_record_that_differs_on_disk(self):
        record = self.video.parent / "render-result.json"
        record.write_text(json.dumps({**self.f.result, "glyph_coverage": "FAIL"}))
        self.reject("MEDIA_RESULT_RECORD_MISMATCH")

    def test_rejects_a_missing_or_fake_preview(self):
        preview = self.video.parent / "preview.png"
        preview.write_bytes(b"GIF89a")
        self.reject("MEDIA_PREVIEW_MISSING", guard="media-preview")
        preview.unlink()
        self.reject("MEDIA_PREVIEW_MISSING", guard="media-preview")

    def test_rejects_foreign_fonts_and_unverified_layout(self):
        self.reject(
            "MEDIA_FONT_PROVENANCE_MISMATCH",
            self.record({**self.f.result, "font_sha256": "c" * 64}),
            "media-font",
        )
        self.reject(
            "MEDIA_LAYOUT_UNVERIFIED",
            self.record({**self.f.result, "safe_area": {"result": "FAIL"}}),
            "media-layout",
        )
        self.reject(
            "MEDIA_LAYOUT_UNVERIFIED",
            self.record({**self.f.result, "glyph_coverage": "FAIL"}),
            "media-layout",
        )

    def record(self, result):
        (self.video.parent / "render-result.json").write_bytes(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2).encode()
        )
        return result

    def rewrite(self, data):
        import hashlib

        self.video.write_bytes(data)
        return self.record(
            changed(
                {**self.f.result, "video_sha256": hashlib.sha256(data).hexdigest()}, bytes=len(data)
            )
        )


class OwnedRenderAgentTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.OwnedContractAdapterTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def run_role(self, role, result=None):
        f = self.f
        settings = f.artifacts()
        result = result or f.result
        return f.stage(
            role,
            result,
            lambda: run_render_check(role, f.repo, f.job, f.token, 1, result, settings),
        )

    def test_quality_requires_every_render_check_in_order(self):
        f = self.f
        f.rendered()
        for role in RENDER_CHECKS[:-1]:
            self.run_role(role)
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_DEPENDENCY_INVALID", msg="guard:quality-after-judge"
        ):
            f.stage("quality", f.result, lambda: f.receipt)

    def test_rejected_render_check_fails_durably_and_quality_never_runs(self):
        f = self.f
        loud = changed(f.result, audio_qa={**f.result["qa"]["audio_qa"], "true_peak_dbtp": 0.0})
        f.result = loud
        f.rendered()
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_CHECK_REJECTED", msg="guard:render-check-rejected-code"
        ):
            self.run_role("voice", loud)
        self.assertEqual(f.status("voice"), "FAILED")
        with self.assertRaisesRegex(ContractViolation, "CONTRACT_DEPENDENCY_INVALID"):
            self.run_role("media_verification", loud)
        with self.assertRaisesRegex(ContractViolation, "CONTRACT_DEPENDENCY_INVALID"):
            f.stage("quality", loud, lambda: f.receipt)

    def test_render_check_cannot_rewrite_the_result(self):
        f = self.f
        f.rendered()
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_OUTPUT_INVALID", msg="guard:render-pass-through"
        ):
            f.stage("voice", f.result, lambda: {**f.result, "glyph_coverage": "PASS "})

    def test_render_check_refuses_another_attempt_result(self):
        f = self.f
        f.rendered()
        foreign = {
            **f.result,
            "attempt": 2,
            "artifact": f"renders/{f.job}/attempts/2/output/did-you-know.mp4",
        }
        with self.assertRaisesRegex(
            ContractViolation, "CONTRACT_INPUT_INVALID", msg="guard:render-check-binding"
        ):
            f.stage("voice", foreign, lambda: foreign)

    def test_judge_rejects_tampered_earlier_evidence(self):
        for role, column, value, code in (
            ("proofreading", "output_sha", "0" * 64, "JUDGE_CONTENT_LINEAGE_INVALID"),
            ("design", "output_sha", "1" * 64, "JUDGE_RENDER_LINEAGE_INVALID"),
            ("voice", "input_sha", "2" * 64, "JUDGE_RENDER_LINEAGE_INVALID"),
            ("memory", "status", "FAILED", "JUDGE_EVIDENCE_INCOMPLETE"),
            ("rotation", "plan_sha", "3" * 64, "JUDGE_PLAN_CHANGED"),
        ):
            with self.subTest(role=role, column=column):
                f = fixtures.OwnedContractAdapterTests()
                f.setUp()
                self.addCleanup(f.doCleanups)
                f.rendered()
                settings = f.artifacts()
                for name in RENDER_CHECKS[:-1]:
                    f.stage(
                        name,
                        f.result,
                        lambda name=name: run_render_check(
                            name, f.repo, f.job, f.token, 1, f.result, settings
                        ),
                    )
                with f.repo.transaction() as db:
                    db.execute(
                        "UPDATE owned_agent_contracts SET "
                        + column
                        + "=? WHERE job_id=? AND role=?",
                        (value, f.job, role),
                    )
                with (
                    f.repo.connect() as db,
                    self.assertRaisesRegex(AgentRejected, code, msg="guard:judge-" + code),
                ):
                    judge(db, f.job, 1, f.result)

    def test_every_render_check_passes_the_result_through_unchanged(self):
        f = self.f
        f.adopted()
        with f.repo.connect() as db:
            rows = {
                r["role"]: dict(r)
                for r in db.execute("SELECT * FROM owned_agent_contracts WHERE job_id=?", (f.job,))
            }
        for role in RENDER_CHECKS:
            self.assertEqual(
                (rows[role]["status"], rows[role]["input_sha"], rows[role]["output_sha"]),
                ("SUCCEEDED", digest(f.result), digest(f.result)),
            )

    def test_delivery_gate_rechecks_every_render_check_row(self):
        for role in RENDER_CHECKS:
            for column, value, code in (
                ("status", "FAILED", "CONTRACT_DELIVERY_BLOCKED"),
                ("output_sha", "0" * 64, "CONTRACT_RENDER_CHECK_CHAIN_INVALID"),
            ):
                with self.subTest(role=role, column=column):
                    f = fixtures.OwnedContractAdapterTests()
                    f.setUp()
                    self.addCleanup(f.doCleanups)
                    f.adopted()
                    with f.repo.connect() as db:
                        require_delivery_contract(db, f.job, f.result)
                    with f.repo.transaction() as db:
                        db.execute(
                            "UPDATE owned_agent_contracts SET "
                            + column
                            + "=? WHERE job_id=? AND role=?",
                            (value, f.job, role),
                        )
                    with (
                        f.repo.connect() as db,
                        self.assertRaisesRegex(
                            ContractViolation, code, msg="guard:delivery-render-chain"
                        ),
                    ):
                        require_delivery_contract(db, f.job, f.result)


class RenderCheckReconciliationTests(unittest.TestCase):
    def test_new_owner_refuses_reconciliation_when_a_render_check_row_was_tampered(self):
        from snap_bot.owned_contract_adapter import reconcile_stage
        from studio_core.jobs import LeaseLost

        f = fixtures.OwnedContractAdapterTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.design()
        settings = f.artifacts()
        for state in ("RESEARCHING", "PROCESSING", "DOWNLOADING", "DESIGNING", "RENDERING"):
            f.repo.transition(f.job, f.token, state)

        def adopt_then_lose():
            f.repo.record_stage(
                f.job, f.token, 1, "design", f.result["video_sha256"], f.result["artifact"]
            )
            f.quality_receipt()
            f.now += f.config.lease_seconds + 1
            f.new = f.repo.claim(job_id=f.job)
            return f.receipt

        with self.assertRaises(LeaseLost):
            f.stage("quality", f.result, adopt_then_lose)
        with f.repo.transaction() as db:
            db.execute(
                "UPDATE owned_agent_contracts SET output_sha=? WHERE job_id=? AND role='media_verification'",
                ("2" * 64, f.job),
            )
        with self.assertRaisesRegex(
            ContractViolation,
            "CONTRACT_RENDER_CHECK_CHAIN_INVALID",
            msg="guard:reconcile-render-chain",
        ):
            reconcile_stage(
                f.repo,
                f.job,
                f.new["lease_token"],
                2,
                "quality",
                1,
                settings=settings,
                result=f.result,
            )
        self.assertEqual(f.status("quality"), "RUNNING")


if __name__ == "__main__":
    unittest.main()
