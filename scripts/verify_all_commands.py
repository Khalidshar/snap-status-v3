#!/usr/bin/env python3
"""On-demand production-path verification. No delivery, approval, timers or secrets.

Run: sudo .venv/bin/python scripts/verify_all_commands.py
Only this file's private worker modes run inside transient systemd sandboxes.
"""

import argparse
import hashlib
import html
import json
import math
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path("/var/lib/madar/verification")
APPS = ("snap", "status")
SACRED = {"verse", "adhkar_am", "adhkar_pm", "dua", "hadith", "fast", "friday", "kahf"}
# These are platform transports, not source download clients.
DELIVERY_MODULES = (
    "snap_bot.telegram_egress",
    "status_bot.whatsapp_egress",
    "status_bot.whatsapp_outbox",
)
TEXT_EXPECTED = {
    "status": ("الحالة", "الإصدار", "واتساب"),
    "report": ("تقرير", "رندر", "واتساب"),
    "last": ("آخر", "واتساب"),
    "agents": ("الوكلاء",),
    "schedule": ("الجدولة",),
    "preview": ("لا يوجد مقطع",),
    "send": ("لا يوجد مقطع", "لا يملك مسار"),
    "cancel": ("لا يوجد مقطع", "لا يملك مسار"),
    "halt": ("موقوف",),
    "unhalt": ("تم تشغيل الإرسال",),
    "menu": ("القائمة",),
}
REASONS = {
    "VERIFY_AGENT_CHAIN_INCOMPLETE_SEE_AGENTS_JSON": (
        "لم تُنفّذ الوكلاء الثمانية عشر؛ راجع سجل الوكلاء"
    ),
    "VERIFY_SACRED_POLICY_NOT_SILENT_SACRED": "المقطع الديني لا يحمل سياسة الصمت المعتمدة",
    "VERIFY_SACRED_OR_SILENT_AUDIO_AUDIBLE": "قياس الصوت كشف صوتًا في مقطع يلزم صمته",
    "VERIFY_GREEN_FRAME_PIXEL_MISMATCH": "الإطار الظاهر لا يطابق الإطار الأخضر المعتمد",
    "VERIFY_NEWS_ATTRIBUTION_NOT_RENDERED": "لم يثبت ظهور مصدر الخبر ووقته في المقطع",
    "VERIFY_COMMAND_UNIMPLEMENTED_OR_FAILED": "الأمر غير مكتمل أو أعاد رد فشل",
    "VERIFY_TEXT_SEMANTICS_NOT_PROVEN": "لم يثبت أن الرد يطابق وظيفة الأمر",
    "VERIFY_VIDEO_HASH": "بصمة المقطع لا تطابق نتيجة الإخراج",
    "VERIFY_PRODUCTION_UNIT_NOT_INSTALLED": "وحدة تشغيل الأمر غير مثبتة على السيرفر",
    "VERIFY_SAFE_AREA": "لم ينجح فحص المساحة الآمنة",
    "NewsUnavailable": "لا يوجد خبر حديث مناسب من مصدر معتمد",
    "CalledProcessError": "فشل تشغيل المسار المعزول؛ راجع مرحلة الفشل وحالة الوحدات",
    "TimeoutExpired": "انتهت مهلة تشغيل الأمر",
    "PermissionError": "تعذر الوصول ضمن صلاحيات العزل",
    "FileNotFoundError": "ملف أو أداة مطلوبة غير موجودة",
}


def reason_text(code: str) -> str:
    return REASONS.get(code, "فشل التحقق؛ رمز التشخيص: " + code)


def dump(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run(argv: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, check=True, timeout=timeout)  # noqa: S603


def blocked(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("VERIFY_DELIVERY_OR_SIDE_EFFECT_BLOCKED")


def install_guards(app: str) -> None:
    """Defence in depth: no credentials, platform transport, direct IP or other proxy."""
    import importlib

    for name in DELIVERY_MODULES:
        module = importlib.import_module(name)
        if hasattr(module, "api"):
            module.api = blocked
        for cls in ("GraphTransport", "WhatsAppOutbox"):
            if hasattr(module, cls):
                for method in ("send_text", "send_video", "upload", "deliver", "send"):
                    if hasattr(getattr(module, cls), method):
                        setattr(getattr(module, cls), method, blocked)
    allowed = {
        f"/var/lib/madar/{app}/news/egress.sock",
        f"/run/madar/{app}-video.sock",
    }

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "socket.__new__" and args[1] != socket.AF_UNIX:
            raise PermissionError("VERIFY_IP_BLOCKED")
        if event == "socket.connect" and args[1] not in allowed:
            raise PermissionError("VERIFY_SOCKET_BLOCKED")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0]))
            if path.suffix == ".env" or path.is_relative_to("/etc/madar"):
                raise PermissionError("VERIFY_SECRET_READ_BLOCKED")

    sys.addaudithook(audit)


def unit_command(code: list[str], work: Path, *, offline: bool) -> list[str]:
    """No shell, inherited credentials, existing units, timers or background work."""
    argv = [
        "/usr/bin/systemd-run",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        "--unit=madar-verify-" + uuid.uuid4().hex,
        "--property=Type=exec",
        "--property=PrivateNetwork=yes",
        "--property=RestrictAddressFamilies=AF_UNIX",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes",
        "--property=UMask=0077",
        "--property=RuntimeMaxSec=1800",
        "--property=TimeoutStopSec=15",
        "--property=WorkingDirectory=" + str(ROOT),
        "--property=ReadWritePaths=" + str(work) + " /tmp /var/lib/madar/render-work",
        "--property=InaccessiblePaths=-/etc/madar -/opt/studio-v2",
        "--setenv=PYTHONDONTWRITEBYTECODE=1",
        "--setenv=MADAR_QURAN_AUDIO=0",
        "--setenv=MADAR_LIVE_NEWS=1",
    ]
    if offline:
        argv += [
            "--property=NoNewPrivileges=yes",
            "--property=InaccessiblePaths=-/run/madar -/var/lib/madar/snap/news/egress.sock "
            "-/var/lib/madar/status/news/egress.sock",
        ]
    return [*argv, sys.executable, str(Path(__file__).resolve()), *code]


def renderer(app: str, route: str, tag: str, work: Path) -> None:
    launch(unit_command(["--render", app, route, tag, str(work)], work, offline=True))


def launch(argv: list[str]) -> None:
    unit = next(v.removeprefix("--unit=") for v in argv if v.startswith("--unit="))
    if not re.fullmatch("madar-verify-[a-f0-9]{32}", unit):
        raise ValueError("VERIFY_UNIT_SCOPE")
    try:
        run(argv, 1815)
    finally:
        subprocess.run(  # noqa: S603 -- only our uniquely named verification unit
            ["/usr/bin/systemctl", "stop", unit], capture_output=True, timeout=30, check=False
        )


def source(app: str, key: str, work: Path) -> dict[str, Any]:
    from studio_core.network import NetworkClient
    from studio_core.sources.daily_brief import prepare
    from studio_core.sources.live import LiveNews
    from studio_core.sources.news import collect

    service = LiveNews(app + "-bot", ROOT / f"config/{app}/news.toml", work / "news.sqlite")
    if key == "brief":
        value = prepare(service, uuid.uuid4().hex)
        if value is None:
            raise ValueError("VERIFY_BRIEF_NO_CONTENT")
        return value
    return collect(key, NetworkClient(service.config), time.time())["content"]


def prepare_news_media(app: str, content: dict[str, Any], directory: Path) -> None:
    from studio_core.network.client import NetworkClient
    from studio_core.network.video import VideoPolicy
    from studio_core.presentation import footage
    from studio_core.sources import event_video

    policy = json.loads((ROOT / "config/media/source-video.json").read_text())
    sock = Path(f"/run/madar/{app}-video.sock")
    client = NetworkClient(VideoPolicy(sock))  # same production source-video gateway
    if event_video.prepare(content, directory / "media", client, policy) is None:
        footage.prepare(
            {**content, "body": content["headline"]}, directory / "media/background", sock
        )


def make_status(work: Path) -> Any:
    from status_bot.content_clips import initialize
    from studio_core.config import Config
    from studio_core.jobs import Repository

    repo = Repository(
        replace(Config.load(ROOT / "config/status/commands.toml"), data_root=work / "state")
    )
    initialize(repo)
    # Only build() is called: it needs a local binding for its private history query.
    # No WhatsAppOutbox instance, credentials, window, webhook or delivery queue.
    return SimpleNamespace(repo=repo, identity=SimpleNamespace(binding="verification-local"))


def produce(app: str, key: str, work: Path) -> tuple[dict, dict, Path]:
    from studio_core.sources.news import TITLES

    if app == "status":
        from status_bot.content_clips import build

        outbox = make_status(work)
        tag = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        content, result, raw = build(
            outbox,
            {"tag": tag},
            key,
            source=source(app, key, work) if key in TITLES or key == "brief" else None,
            root=work / "clips",
            start=lambda unit: renderer(app, "status", tag, work),
        )
        video = work / "clips" / tag / result["video_path"]
        if hashlib.sha256(raw).hexdigest() != hashlib.sha256(video.read_bytes()).hexdigest():
            raise ValueError("VERIFY_BUILD_BYTES_CHANGED")
        return content, result, video
    if key not in TITLES and key != "brief":
        from snap_bot.owner_commands import RENDER_KINDS
        from snap_bot.server_preview import render_bundle

        preview = render_bundle(
            work / "bundle",
            {"run_id": "server-verification", "head_sha": revision(), "kind": RENDER_KINDS[key]},
        )
        content = json.loads(
            (work / "bundle/state/renders" / preview["job_id"] / "input/content.json").read_text()
        )
        return content, preview["render"], work / "bundle/did-you-know.mp4"
    content = source(app, key, work)
    tag = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    directory = work / "clips" / tag
    directory.mkdir(parents=True)
    dump(directory / "content.json", content)
    if key != "brief":
        prepare_news_media(app, content, directory)
    # Same application render_event() functions as the production oneshot units.
    renderer(app, "brief" if key == "brief" else "news", tag, work)
    result = json.loads((directory / "result.json").read_text())
    video = directory / ("video.mp4" if key == "brief" else "render/video.mp4")
    return content, result, video


def revision() -> str:
    manifest = ROOT / "madar-release.json"
    if manifest.exists():
        value = json.loads(manifest.read_text())["commit"]
    else:
        value = (
            run(["/usr/bin/git", "-C", str(ROOT), "rev-parse", "HEAD"], 15).stdout.decode().strip()
        )
    if not re.fullmatch("[0-9a-f]{40}", value):
        raise ValueError("VERIFY_REVISION_INVALID")
    return value


def probe(video: Path) -> dict:
    return json.loads(
        run(
            [
                "/usr/bin/ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(video),
            ],
            30,
        ).stdout
    )


def max_volume(video: Path, *, start: float = 0, duration: float | None = None) -> float:
    args = ["/usr/bin/ffmpeg", "-hide_banner", "-nostdin", "-ss", str(start), "-i", str(video)]
    if duration is not None:
        args += ["-t", str(duration)]
    result = run([*args, "-vn", "-af", "volumedetect", "-f", "null", "-"], 900)
    match = re.search(rb"max_volume:\s*(-?inf|[-+0-9.]+) dB", result.stderr)
    if match is None:
        raise ValueError("VERIFY_AUDIO_MEASUREMENT_MISSING")
    return float(match[1])


def silent(video: Path, **kwargs: Any) -> dict:
    if not any(s.get("codec_type") == "audio" for s in probe(video)["streams"]):
        return {"policy": "silent_sacred", "audio_stream": False}
    peak = max_volume(video, **kwargs)
    if math.isnan(peak) or peak > -80:
        raise ValueError("VERIFY_SACRED_OR_SILENT_AUDIO_AUDIBLE")
    return {"peak_dbfs": peak if math.isfinite(peak) else None, "silent": True}


def audio_check(key: str, content: dict, result: dict, video: Path) -> dict:
    if key in SACRED:
        if result.get("qa", {}).get("audio") != "silent_sacred":
            raise ValueError("VERIFY_SACRED_POLICY_NOT_SILENT_SACRED")
        return silent(video)
    from studio_core.presentation.command_cards import profile
    from studio_core.presentation.fact_audio import measure_audio
    from studio_core.presentation.media_checks import check_voice
    from studio_core.renderer import run as media_run

    if key == "brief":
        # Check the verse interval in the FINAL concatenated brief, not only a source file.
        dates = next(video.parent.rglob("dates/video.mp4"))
        faith = next(video.parent.rglob("faith/renders/*/attempts/1/output/did-you-know.mp4"))
        offset = float(probe(dates)["format"]["duration"])
        length = float(probe(faith)["format"]["duration"])
        if result.get("audio_policy") != "BRIEF_MIX":
            raise ValueError("VERIFY_BRIEF_AUDIO_POLICY")
        # AAC joins may add a boundary frame. The source is checked in full as well.
        silent(faith)
        return {"faith": silent(video, start=offset + 0.1, duration=max(0.1, length - 0.2))}
    policy = result.get("audio_policy")
    if content.get("type") == "news_review":
        if policy == "SILENT":
            return silent(video)
        if policy != "ORIGINAL_SOURCE":
            raise ValueError("VERIFY_NEWS_AUDIO_POLICY")
        return {"policy": policy, "validated_by": "production original-source manifest"}
    check_voice(result)
    measured = measure_audio(video, profile(), media_run)
    if not -19 <= measured["integrated_lufs"] <= -17 or measured["true_peak_dbtp"] > -1.5:
        raise ValueError("VERIFY_AMBIENT_MEASUREMENT_FAILED")
    return measured


def compare_frame(rgb: bytes, rgba: bytes, box: dict) -> dict:
    if len(rgb) != 1080 * 1920 * 3 or len(rgba) != 1080 * 1920 * 4:
        raise ValueError("VERIFY_FRAME_SIZE")
    total, bad, error = 0, 0, 0
    for y in range(4, 1916, 4):
        for x in range(4, 1076, 4):
            if box["x"] - 4 <= x <= box["x"] + box["width"] + 4 and (
                box["y"] - 4 <= y <= box["y"] + box["height"] + 4
            ):
                continue
            i = y * 1080 + x
            if rgba[4 * i + 3] != 255:
                continue
            delta = max(abs(rgb[3 * i + c] - rgba[4 * i + c]) for c in range(3))
            error += delta
            bad += delta > 35
            total += 1
    if total < 10000 or error / total > 12 or bad / total > 0.03:
        raise ValueError("VERIFY_GREEN_FRAME_PIXEL_MISMATCH")
    return {"sampled_pixels": total, "mean_error": round(error / total, 3)}


def frame_check(video: Path, duration: float, screenshot: Path) -> dict:
    from studio_core.presentation.brand_frame import load_brand

    brand = load_brand()
    run(
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-ss",
            str(duration / 2),
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-threads",
            "1",
            "-n",
            str(screenshot),
        ],
        60,
    )
    rgba = run(
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-i",
            str(brand["overlay_path"]),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba",
            "-threads",
            "1",
            "-",
        ],
        60,
    ).stdout
    checks = []
    for at in (min(1.0, duration / 4), duration / 2, max(0.0, duration - 0.2)):
        args = [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-ss",
            str(at),
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-threads",
            "1",
        ]
        rgb = run([*args, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], 60).stdout
        checks.append({"at": at, **compare_frame(rgb, rgba, brand["clip_box"])})
    return {"overlay_sha256": brand["overlay"]["sha256"], "samples": checks}


def agent_evidence(work: Path, app: str) -> dict:
    from studio_core.observability.agent_inventory import ROLE_IDS

    found: dict[str, str] = {}
    for path in work.rglob("jobs.sqlite"):
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            for table in ("agent_runs", "owned_agent_contracts"):
                if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    for role, status in db.execute(f"SELECT role,status FROM {table}"):  # noqa: S608
                        if role not in found or status not in {"SUCCEEDED", "PASS"}:
                            found[role] = status
    rows = [{"role": role, "state": found.get(role, "NOT_EXECUTED")} for role in ROLE_IDS]
    return {
        "app": app,
        "roles": rows,
        "executed": len(found),
        "all_18_passed": all(r["state"] in {"SUCCEEDED", "PASS"} for r in rows),
    }


def attribution(key: str, content: dict, work: Path) -> dict:
    from studio_core.presentation.fact_layout import display_text

    news = [content[k] for k in ("saudi", "tech")] if key == "brief" else [content]
    labels = {p.read_text() for p in work.rglob("label-*.txt")}
    for item in news:
        for text in ("المصدر: " + item["source"], item["published_label"]):
            if display_text(text, "arabic") not in labels:
                raise ValueError("VERIFY_NEWS_ATTRIBUTION_NOT_RENDERED")
    return {"source_and_time_in_render_inputs": True}


def media_checks(app: str, key: str, work: Path, content: dict, result: dict, video: Path) -> dict:
    checks: dict[str, Any] = {}

    def check(name: str, callback: Any) -> None:
        try:
            checks[name] = {"passed": True, "evidence": callback()}
        except Exception as error:
            checks[name] = {"passed": False, "reason": safe_error(error)}

    def integrity() -> dict:
        if hashlib.sha256(video.read_bytes()).hexdigest() != result.get("video_sha256"):
            raise ValueError("VERIFY_VIDEO_HASH")
        data = probe(video)
        streams = [s for s in data["streams"] if s.get("codec_type") == "video"]
        duration = float(data["format"]["duration"])
        if len(streams) != 1 or (streams[0]["width"], streams[0]["height"]) != (1080, 1920):
            raise ValueError("VERIFY_VIDEO_PROFILE")
        if not 0 < duration <= 180 or not 0 < video.stat().st_size <= 50_000_000:
            raise ValueError("VERIFY_VIDEO_DURATION_OR_SIZE")
        if key == "brief" and not 30 <= duration <= 60:
            raise ValueError("VERIFY_BRIEF_DURATION")
        run(
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-nostdin",
                "-i",
                str(video),
                "-f",
                "null",
                "-",
            ],
            900,
        )
        if result.get("safe_area", {}).get("result") != "PASS":
            raise ValueError("VERIFY_SAFE_AREA")
        return {"duration": duration, "sha256": result["video_sha256"]}

    def content_check() -> dict:
        from studio_core.presentation.did_you_know import validate_content
        from studio_core.sources.daily_brief import validate_brief
        from studio_core.sources.news import validate

        if key == "brief":
            validate_brief(content, time.time())
        elif content.get("type") == "news_review":
            validate(content, time.time())
        else:
            validate_content(content)
        return {
            "hash": content["content_hash"],
            "source": content.get("source"),
            "time": content.get("published_label"),
            "dates": content.get("dates"),
        }

    def agents() -> dict:
        evidence = agent_evidence(work, app)
        dump(work / "agents.json", evidence)
        if not evidence["all_18_passed"]:
            raise ValueError("VERIFY_AGENT_CHAIN_INCOMPLETE_SEE_AGENTS_JSON")
        return evidence

    def installed_unit() -> dict:
        from studio_core.sources.news import TITLES

        if app == "status":
            name = "madar-v3-status-content@.service"
        elif key == "brief":
            name = "madar-v3-snap-brief@.service"
        elif key in TITLES:
            name = "madar-v3-snap-news-clip@.service"
        else:
            name = "madar-v3-snap-render@.service"
        state = (
            run(
                [
                    "/usr/bin/systemctl",
                    "show",
                    name.replace("@.service", "@verification.service"),
                    "--property=LoadState",
                    "--value",
                ],
                30,
            )
            .stdout.decode()
            .strip()
        )
        if state != "loaded":
            raise ValueError("VERIFY_PRODUCTION_UNIT_NOT_INSTALLED")
        return {"unit": name, "load_state": state, "started": False}

    check("integrity", integrity)
    check("content_and_sources", content_check)
    if key == "brief" or content.get("type") == "news_review":
        check("visible_source_and_time", lambda: attribution(key, content, work))
    check("audio", lambda: audio_check(key, content, result, video))
    check(
        "green_frame",
        lambda: frame_check(video, float(probe(video)["format"]["duration"]), work / "frame.png"),
    )
    check("agents_18", agents)
    check("production_unit", installed_unit)
    return checks


def safe_error(error: Exception) -> str:
    # Never copy exception messages (upstream errors can contain URLs/credentials).
    message = str(error)
    if re.fullmatch(r"VERIFY_[A-Z0-9_]+", message):
        return message
    return type(error).__name__


def text_reply(app: str, key: str, work: Path) -> dict:
    from studio_core import commands

    label = commands.BY_KEY[key].label
    if app == "snap":
        from snap_bot import owner_commands as owner
        from snap_bot.server_preview import Runtime

        studio = owner.Studio(
            Runtime(work / "runtime"), start=blocked, pending_path=work / "pending.json"
        )
        with patch.object(owner, "HALT", work / "HALT"):
            reply = studio.answer(label)
    else:
        from status_bot import command_control, tool_commands
        from status_bot import owner_commands as owner
        from status_bot.server_preview import Runtime

        runtime = Runtime(work / "runtime")
        outbox = make_status(work)
        with patch.object(command_control, "HALT", work / "HALT"):
            if key in {"halt", "unhalt"}:
                reply = command_control.change(key == "halt", runtime, uuid.uuid4().hex)
            elif key == "agents":
                reply = tool_commands.status(outbox)
            elif key in tool_commands.NAMES:
                reply = tool_commands.execute(key, outbox)
            else:
                reply = owner.answer(label, runtime)
    if not isinstance(reply, str) or not reply.strip():
        raise ValueError("VERIFY_TEXT_EMPTY_OR_MEDIA")
    dump(work / "reply.json", {"reply": reply, "state": "isolated verification state"})
    if any(s in reply for s in ("لم يصل", "لا يملك مسار", "تعذر إكمال", "تعذّر")):
        raise ValueError("VERIFY_COMMAND_UNIMPLEMENTED_OR_FAILED")
    expected = TEXT_EXPECTED.get(key)
    if expected is None or not any(token in reply for token in expected):
        raise ValueError("VERIFY_TEXT_SEMANTICS_NOT_PROVEN")
    return {"reply": reply, "scope": "real handler with fresh isolated state"}


def worker(app: str, key: str, work: Path) -> int:
    from status_bot.content_clips import KINDS
    from studio_core.commands import BY_KEY
    from studio_core.sources.news import TITLES

    if app not in APPS or key not in BY_KEY:
        raise ValueError("VERIFY_UNKNOWN_COMMAND")
    os.environ["MADAR_QURAN_AUDIO"] = "0"
    install_guards(app)
    row: dict[str, Any] = {
        "app": app,
        "key": key,
        "number": BY_KEY[key].number,
        "label": BY_KEY[key].label,
        "external_delivery": False,
    }
    try:
        if key in KINDS or key in TITLES or key == "brief":
            content, result, video = produce(app, key, work)
            dump(work / "content.json", content)
            dump(work / "render.json", result)
            row["video"] = str(video.relative_to(work))
            row["checks"] = media_checks(app, key, work, content, result, video)
            row["passed"] = all(c["passed"] for c in row["checks"].values())
            row["frame"] = "frame.png" if (work / "frame.png").exists() else None
        else:
            row["checks"] = {"text": {"passed": True, "evidence": text_reply(app, key, work)}}
            row["passed"] = True
    except Exception as error:
        row.update(passed=False, reason=safe_error(error))
        # A production quality gate may reject AFTER rendering. Keep a still for review.
        candidates = list(work.rglob("did-you-know.mp4")) + list(work.rglob("video.mp4"))
        if candidates:
            try:
                video = candidates[0]
                run(
                    [
                        "/usr/bin/ffmpeg",
                        "-v",
                        "error",
                        "-nostdin",
                        "-i",
                        str(video),
                        "-frames:v",
                        "1",
                        "-threads",
                        "1",
                        "-n",
                        str(work / "frame.png"),
                    ],
                    60,
                )
                row["diagnostic_video"] = str(video.relative_to(work))
            except Exception as preview_error:
                row["diagnostic_frame_error"] = safe_error(preview_error)
    dump(work / "verification.json", row)
    return 0  # Per-command failures belong in the report; continue every command.


def render_worker(app: str, route: str, tag: str, work: Path) -> int:
    if app not in APPS or not re.fullmatch("[a-f0-9]{64}", tag):
        raise ValueError("VERIFY_RENDER_SCOPE")
    install_guards(app)
    if route == "status" and app == "status":
        from status_bot.content_clips import render_event
    elif route == "brief" and app == "snap":
        from snap_bot.brief import render_event
    elif route == "news" and app == "snap":
        from snap_bot.news_clips import render_event
    else:
        raise ValueError("VERIFY_RENDER_ROUTE")
    render_event(tag, root=work / "clips")
    return 0


def report(output: Path, rows: list[dict]) -> bool:
    from studio_core.commands import COMMANDS

    stamp = datetime.now(ZoneInfo("Asia/Riyadh")).strftime("%Y-%m-%d %I:%M %p")
    stamp = stamp.replace("AM", "ص").replace("PM", "م")
    passed = sum(r.get("passed") is True for r in rows)
    total = len(COMMANDS) * len(APPS)
    dump(
        output / "report.json",
        {
            "schema": 1,
            "created_at": stamp,
            "revision": revision(),
            "external_delivery": False,
            "expected": total,
            "completed": len(rows),
            "complete": len(rows) == total,
            "passed": passed,
            "failed": len(rows) - passed,
            "commands": rows,
        },
    )
    body = []
    for r in rows:
        failures = [
            name + ": " + reason_text(c.get("reason", ""))
            for name, c in r.get("checks", {}).items()
            if not c["passed"]
        ]
        reason = reason_text(r["reason"]) if r.get("reason") else "؛ ".join(failures)
        reason = reason or "اجتاز جميع الفحوص"
        frame = r.get("frame")
        link = f'<a href="{html.escape(frame, quote=True)}">صورة المقطع</a>' if frame else "—"
        body.append(
            "<tr>"
            + "".join(
                "<td>" + html.escape(str(v)) + "</td>"
                for v in (
                    "سناب" if r["app"] == "snap" else "الحالات",
                    r["number"],
                    r["label"],
                    "ناجح" if r.get("passed") else "فاشل",
                    reason,
                )
            )
            + "<td>"
            + link
            + "</td></tr>"
        )
    (output / "report.html").write_text(
        '<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8">'
        "<title>التحقق الشامل للبوتين</title><style>body{font:17px system-ui;background:#092d24;"
        "color:#f6eede;padding:30px}table{border-collapse:collapse;width:100%}"
        "td,th{border:1px solid #697b65;padding:12px}a{color:#efce81}</style>"
        f"<h1>التحقق الشامل للبوتين</h1><p>{passed} ناجح — {len(rows) - passed} فاشل"
        f" — {total - len(rows)} لم يُفحص بعد</p>"
        "<p>لم يُرسل أي محتوى. الردود تستخدم حالة تحقق مستقلة. الصور فحص بصري مساعد؛ "
        "لا تعني نجاح فحص لم يُنفّذ. رموز الأسباب وتفاصيل الفحوص في report.json.</p>"
        "<table><tr><th>البوت</th><th>الرقم</th><th>الأمر</th><th>النتيجة</th><th>السبب</th>"
        "<th>الصورة</th></tr>" + "".join(body) + "</table></html>",
        encoding="utf-8",
    )
    return passed == len(rows) == total


def main() -> int:
    parser = argparse.ArgumentParser(description="تحقق شامل للبوتين دون إرسال")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", nargs=3, metavar=("APP", "KEY", "WORK"), help=argparse.SUPPRESS)
    parser.add_argument("--render", nargs=4, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker or args.render:
        mode = args.worker or args.render
        work = Path(mode[-1]).absolute()
        if not work.is_relative_to(OUTPUT) or any(p.is_symlink() for p in (work, *work.parents)):
            parser.error("VERIFY_WORK_SCOPE")
    if args.worker:
        app, key, work = args.worker
        return worker(app, key, Path(work))
    if args.render:
        app, route, tag, work = args.render
        return render_worker(app, route, tag, Path(work))
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("يلزم لينكس وsystemd وتشغيل الأمر عبر sudo")
    from studio_core.commands import COMMANDS

    output = args.output or OUTPUT / (
        datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    )
    output = output.absolute()
    if not output.is_relative_to(OUTPUT) or any(p.is_symlink() for p in (output, *output.parents)):
        parser.error("مجلد التقرير يجب أن يكون جديدًا داخل /var/lib/madar/verification")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    (output / "frames").mkdir()
    print("مجلد التقرير: " + str(output), flush=True)
    rows = []
    for app in APPS:
        for command in COMMANDS:
            work = output / app / f"{command.number:02d}-{command.key}"
            work.mkdir(mode=0o700, parents=True)
            try:
                # systemd manager creates a fresh environment. Never forward the shell env.
                launch(
                    unit_command(["--worker", app, command.key, str(work)], work, offline=False),
                )
                row = json.loads((work / "verification.json").read_text())
            except Exception as error:
                row = {
                    "app": app,
                    "key": command.key,
                    "number": command.number,
                    "label": command.label,
                    "passed": False,
                    "reason": safe_error(error),
                    "external_delivery": False,
                }
            if (work / "frame.png").exists():
                target = output / "frames" / f"{app}-{command.number:02d}-{command.key}.png"
                target.write_bytes((work / "frame.png").read_bytes())
                row["frame"] = str(target.relative_to(output))
            rows.append(row)
            report(output, rows)  # checkpoint after each command; keep partial evidence
            print(
                ("سناب" if app == "snap" else "الحالات")
                + " · "
                + command.label
                + " · "
                + ("ناجح" if row.get("passed") else "فاشل"),
                flush=True,
            )
    return 0 if report(output, rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
