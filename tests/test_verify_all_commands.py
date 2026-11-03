"""Verifier safety and failure detection; no production service, source or sending."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/verify_all_commands.py"
SPEC = importlib.util.spec_from_file_location("verify_all_commands", SCRIPT)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


def test_transient_units_have_no_delivery_authority_or_secrets(tmp_path):
    for offline in (False, True):
        argv = verify.unit_command(
            ["--worker", "snap", "verse", str(tmp_path)], tmp_path, offline=offline
        )
        assert "--property=PrivateNetwork=yes" in argv
        assert "--property=RestrictAddressFamilies=AF_UNIX" in argv
        assert "--setenv=MADAR_QURAN_AUDIO=0" in argv
        assert not any("EnvironmentFile" in a or "TG_TOKEN" in a or "WA_TOKEN" in a for a in argv)
        assert not any("send-main" in a or ".timer" in a for a in argv)
        if offline:
            assert any("-/run/madar" in a for a in argv)
            assert any("news/egress.sock" in a for a in argv)


def test_guard_rejects_ip_secret_and_platform_transport_in_separate_process():
    code = """
import importlib.util,socket
from pathlib import Path
spec=importlib.util.spec_from_file_location("v",__import__("sys").argv[1])
v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)
v.install_guards("snap")
checks=0
for action in (lambda:socket.socket(socket.AF_INET),
               lambda:socket.socket(socket.AF_INET6),
               lambda:Path("/etc/madar/quran.env").read_bytes(),
               lambda:__import__("snap_bot.telegram_egress",fromlist=["api"]).api("unused","sendVideo"),
               lambda:socket.socket(socket.AF_UNIX).connect("/run/madar/send.sock")):
    try: action()
    except (PermissionError,RuntimeError): checks+=1
assert checks==5
"""
    result = subprocess.run(  # noqa: S603 -- fixed test subprocess
        [sys.executable, "-c", code, str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_launch_stops_only_its_own_unit_on_failure(tmp_path, monkeypatch):
    argv = verify.unit_command([], tmp_path, offline=False)
    monkeypatch.setattr(verify, "run", lambda *a: (_ for _ in ()).throw(TimeoutError()))
    calls = []
    monkeypatch.setattr(verify.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    with pytest.raises(TimeoutError):
        verify.launch(argv)
    assert calls == [
        [
            "/usr/bin/systemctl",
            "stop",
            next(v.removeprefix("--unit=") for v in argv if v.startswith("--unit=")),
        ]
    ]
    with pytest.raises(ValueError, match="UNIT_SCOPE"):
        verify.launch(["--unit=madar-v3-snap-send-main@fixture"])
    assert len(calls) == 1


def test_missing_or_failed_agents_never_pass(tmp_path):
    from studio_core.observability.agent_inventory import ROLE_IDS

    path = tmp_path / "jobs.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE agent_runs(role TEXT,status TEXT)")
        db.executemany(
            "INSERT INTO agent_runs VALUES(?,'SUCCEEDED')", [(r,) for r in ROLE_IDS[:12]]
        )
    result = verify.agent_evidence(tmp_path, "snap")
    assert result["executed"] == 12 and not result["all_18_passed"]
    assert len(result["roles"]) == 18
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO agent_runs VALUES(?,'SUCCEEDED')", [(r,) for r in ROLE_IDS[12:]]
        )
    assert verify.agent_evidence(tmp_path, "snap")["all_18_passed"]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE agent_runs SET status='FAILED' WHERE role=?", (ROLE_IDS[0],))
    assert not verify.agent_evidence(tmp_path, "snap")["all_18_passed"]


def test_sacred_policy_cannot_be_satisfied_by_ambient_label(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="NOT_SILENT_SACRED"):
        verify.audio_check("verse", {}, {"qa": {"audio": "generated_ambient"}}, tmp_path / "x")
    monkeypatch.setattr(verify, "probe", lambda path: {"streams": [{"codec_type": "audio"}]})
    monkeypatch.setattr(verify, "max_volume", lambda *a, **k: -79.0)
    with pytest.raises(ValueError, match="AUDIO_AUDIBLE"):
        verify.silent(tmp_path / "x")
    monkeypatch.setattr(verify, "max_volume", lambda *a, **k: -91.0)
    assert verify.audio_check("verse", {}, {"qa": {"audio": "silent_sacred"}}, tmp_path / "x")[
        "silent"
    ]


def test_real_silence_and_tone_measurement(tmp_path):
    if not Path("/usr/bin/ffmpeg").exists():
        pytest.skip("ffmpeg required")
    for name, signal in (
        ("silent", "anullsrc=r=48000:cl=stereo"),
        ("tone", "sine=frequency=440:sample_rate=48000"),
    ):
        video = tmp_path / (name + ".mp4")
        subprocess.run(  # noqa: S603 -- fixed local synthetic audio
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                signal,
                "-t",
                "0.3",
                "-c:a",
                "aac",
                str(video),
            ],
            check=True,
        )
        if name == "silent":
            assert verify.silent(video)["silent"]
        else:
            with pytest.raises(ValueError, match="AUDIO_AUDIBLE"):
                verify.silent(video)


def test_frame_pixels_detect_missing_green_identity_and_ignore_content_window():
    rgba = bytes((10, 60, 40, 255)) * (1080 * 1920)
    rgb = bytes((10, 60, 40)) * (1080 * 1920)
    box = {"x": 100, "y": 300, "width": 880, "height": 1200}
    assert verify.compare_frame(rgb, rgba, box)["mean_error"] == 0
    with pytest.raises(ValueError, match="PIXEL_MISMATCH"):
        verify.compare_frame(bytes(len(rgb)), rgba, box)


def test_frame_still_is_saved_even_when_identity_fails(tmp_path, monkeypatch):
    from studio_core.presentation import brand_frame

    image = tmp_path / "frame.png"
    monkeypatch.setattr(brand_frame, "load_brand", lambda: {"overlay_path": "fixture"})

    def fake_run(argv, timeout):
        if str(image) in argv:
            image.write_bytes(b"fixture")
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(verify, "run", fake_run)
    with pytest.raises(KeyError):
        verify.frame_check(tmp_path / "clip.mp4", 10, image)
    assert image.read_bytes() == b"fixture"


def test_report_escapes_content_and_never_claims_partial_run_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "revision", lambda: "a" * 40)
    rows = [{"app": "snap", "number": 1, "label": "<script>bad</script>", "passed": True}]
    assert verify.report(tmp_path, rows) is False
    data = json.loads((tmp_path / "report.json").read_text())
    assert not data["complete"] and data["external_delivery"] is False
    page = (tmp_path / "report.html").read_text()
    assert "<script>" not in page and "لم يُفحص بعد" in page


def test_all_registry_commands_both_apps_and_continue_after_worker_failure(tmp_path, monkeypatch):
    from studio_core import commands

    monkeypatch.setattr(verify, "OUTPUT", tmp_path)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--output", str(tmp_path / "run")])
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    calls = []

    def launch(argv):
        pos = argv.index("--worker")
        app, key, work = argv[pos + 1 : pos + 4]
        calls.append((app, key))
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, ["fixture"])
        verify.dump(
            Path(work) / "verification.json",
            {
                "app": app,
                "key": key,
                "number": commands.BY_KEY[key].number,
                "label": commands.BY_KEY[key].label,
                "passed": True,
            },
        )

    monkeypatch.setattr(verify, "launch", launch)
    monkeypatch.setattr(verify, "revision", lambda: "a" * 40)
    assert verify.main() == 1
    assert calls == [(app, c.key) for app in verify.APPS for c in commands.COMMANDS]
    data = json.loads((tmp_path / "run/report.json").read_text())
    assert data["complete"] and data["failed"] == 1


def test_raw_upstream_error_never_appears_in_report():
    assert verify.safe_error(ValueError("https://upstream.invalid?token=SECRET")) == "ValueError"
    assert verify.safe_error(ValueError("VERIFY_VIDEO_HASH")) == "VERIFY_VIDEO_HASH"


def test_disabled_text_command_is_failure_not_a_green_menu(tmp_path):
    with pytest.raises(ValueError, match="COMMAND_UNIMPLEMENTED"):
        verify.text_reply("status", "dash", tmp_path)


def test_news_attribution_requires_both_actual_rendered_labels(tmp_path):
    from studio_core.presentation.fact_layout import display_text

    content = {"source": "المصدر المعتمد", "published_label": "الاثنين - الثانية مساء"}
    with pytest.raises(ValueError, match="ATTRIBUTION_NOT_RENDERED"):
        verify.attribution("news", content, tmp_path)
    (tmp_path / "label-0.txt").write_text(display_text("المصدر: " + content["source"], "arabic"))
    with pytest.raises(ValueError, match="ATTRIBUTION_NOT_RENDERED"):
        verify.attribution("news", content, tmp_path)
    (tmp_path / "label-1.txt").write_text(display_text(content["published_label"], "arabic"))
    assert verify.attribution("news", content, tmp_path)["source_and_time_in_render_inputs"]


def test_production_status_build_is_used_but_send_never_called(tmp_path, monkeypatch):
    from status_bot import content_clips

    calls = []
    monkeypatch.setattr(verify, "make_status", lambda work: object())
    monkeypatch.setattr(content_clips, "send", verify.blocked)

    def build(outbox, item, key, **kwargs):
        assert key == "verse" and kwargs["source"] is None
        assert kwargs["root"] == tmp_path / "clips"
        kwargs["start"]("ignored-production-unit")
        directory = kwargs["root"] / item["tag"]
        directory.mkdir(parents=True)
        (directory / "video.mp4").write_bytes(b"real-build-placeholder")
        return {"kind": "verse"}, {"video_path": "video.mp4"}, b"real-build-placeholder"

    monkeypatch.setattr(content_clips, "build", build)
    monkeypatch.setattr(verify, "renderer", lambda *a: calls.append(a[:2]))
    content, _result, video = verify.produce("status", "verse", tmp_path)
    assert calls == [("status", "status")]
    assert content["kind"] == "verse" and video.read_bytes() == b"real-build-placeholder"


def test_snap_production_renderer_gets_exact_kind_and_no_approval(tmp_path, monkeypatch):
    from snap_bot import server_preview

    monkeypatch.setattr(verify, "revision", lambda: "a" * 40)

    def render(output, provenance):
        assert provenance["kind"] == "adhkar_am"
        assert "human_approval" not in provenance
        target = output / "state/renders/job/input"
        target.mkdir(parents=True)
        (target / "content.json").write_text('{"kind":"adhkar_am"}')
        return {"job_id": "job", "render": {"qa": "production"}}

    monkeypatch.setattr(server_preview, "render_bundle", render)
    content, result, _video = verify.produce("snap", "adhkar_am", tmp_path)
    assert content["kind"] == "adhkar_am" and result == {"qa": "production"}


def test_worker_marks_missing_source_failure_and_continues_without_fake_clip(tmp_path, monkeypatch):
    from studio_core.sources.news import NewsUnavailable

    monkeypatch.setattr(verify, "install_guards", lambda app: None)
    monkeypatch.setattr(verify, "produce", lambda *a: (_ for _ in ()).throw(NewsUnavailable()))
    assert verify.worker("snap", "news", tmp_path) == 0
    row = json.loads((tmp_path / "verification.json").read_text())
    assert not row["passed"] and row["reason"] == "NewsUnavailable"
    assert "video" not in row


def test_real_approved_transparent_brand_passes_composed_frame_check(tmp_path):
    from studio_core.presentation import brand_frame

    if not Path("/usr/bin/ffmpeg").exists():
        pytest.skip("ffmpeg required")
    source, video = tmp_path / "source.mp4", tmp_path / "approved.mp4"
    verify.run(
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=108x192:r=30:d=0.6",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(source),
        ]
    )
    brand = brand_frame.load_brand()
    verify.run(brand_frame.command("/usr/bin/ffmpeg", source, video, brand, 0.6, False))
    checked = verify.frame_check(video, 0.6, tmp_path / "frame.png")
    assert len(checked["samples"]) == 3
    for sample in checked["samples"]:
        assert sample["background"]["sampled_pixels"] > 10000
        assert sample["identity"]["sampled_pixels"] > 100
    assert (tmp_path / "frame.png").exists()
