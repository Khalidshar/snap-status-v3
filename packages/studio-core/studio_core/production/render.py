"""Chromium timeline, local composition, old QA and one complete rebuild.

This process has no IP sockets. Inputs are immutable files prepared by the broker.
It has no delivery client, tokens, scheduling or AI API.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from studio_core.presentation import brand_frame, footage
from studio_core.presentation.fact_audio import measure_audio, measure_silence
from studio_core.renderer import ensure_glyphs, proof, run

from . import designer
from .isolation import deny_ip_sockets
from .model import ROOT, SACRED, budget, resource, sha, validate


def verify_fonts(p: dict[str, Any]) -> None:
    from studio_core.presentation import quran_font

    quran_font.verify()
    amiri = ROOT / "assets/fonts/amiri"
    amiri_manifest = json.loads((amiri / "manifest.json").read_bytes())
    if (
        amiri_manifest.get("file") != "AmiriQuran.ttf"
        or amiri_manifest.get("sha256")
        != "8fa95faaf7bd18b71789f6f57312003e30c5029218a035e5baf613a0222ab82d"
        or amiri_manifest.get("license_sha256")
        != "a8adc7dde15c05c96a278964b9d9a5aa7b93aadd5b348969ce9bf6efb6cf9ade"
    ):
        raise ValueError("LEGACY_AMIRI_MANIFEST_CHANGED")
    for name, digest in (("AmiriQuran.ttf", "sha256"), ("OFL.txt", "license_sha256")):
        if (amiri / name).is_symlink() or sha((amiri / name).read_bytes()) != amiri_manifest[
            digest
        ]:
            raise ValueError("LEGACY_AMIRI_FONT_CHANGED")
    quran_charset = run(
        ["/usr/bin/fc-query", "--format=%{charset}", str(amiri / "AmiriQuran.ttf")], 5
    ).stdout
    for scene in p["scenes"]:
        if scene.get("verse"):
            ensure_glyphs(scene["body"], quran_charset)
    folder = ROOT / "assets/fonts/notoemoji"
    emoji = json.loads((folder / "manifest.json").read_bytes())
    for file, key in [(emoji["file"], "sha256"), (emoji["license_file"], "license_sha256")]:
        if sha((folder / file).read_bytes()) != emoji[key]:
            raise ValueError("LEGACY_EMOJI_FONT_CHANGED")
    fonts = [
        ROOT / "assets/fonts/arabeyes/ae_AlMohanad.ttf",
        ROOT / "assets/fonts/notonaskharabic/NotoNaskhArabic-Regular.ttf",
        folder / emoji["file"],
    ]
    charset = " ".join(
        run(["/usr/bin/fc-query", "--format=%{charset}", str(f)], 5).stdout for f in fonts
    )
    for s in p["scenes"]:
        # Quran is independently admitted against the official font; inspect all other text.
        text = (
            ("" if s.get("verse") else s["body"])
            + s.get("head", "")
            + s.get("tafsir", "")
            + s.get("punch", "")
            + s.get("source", "")
        )
        ensure_glyphs("".join(c for c in text if c not in "\n\r\ufe0f\u200d"), charset)


TARGET_LUFS = -16.0
LOUDNESS_GATE = (-17.5, -14.5)  # integrated LUFS accepted after the mix
MUSIC_UNDER_VOICE = 0.25  # -12 dB: speech stays ~12 LU above the theme
SIGNATURE_SECONDS = 1.5
SIGNATURE = ((0.05, 659), (0.3, 880), (0.55, 1319))


def audio_mix(p: dict[str, Any], source: Path, work: Path) -> Path:
    """Human recordings stay whole. Never add generated sound to sacred content."""
    seconds = p["duration"]
    args = [
        "/usr/bin/ffmpeg",
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-threads",
        "1",
        "-f",
        "lavfi",
        "-t",
        str(seconds),
        "-i",
        "anullsrc=r=48000:cl=stereo",
    ]
    filters = []
    labels = ["[0:a]"]
    assets = {a["file"]: a["sha256"] for a in p["assets"]}
    clips = list(p["audio"])
    if p["music"]:
        clips.append({"file": p["music"], "at": 0, "seconds": seconds, "music": True})
    # Owner decision 8 Nov 2026 (TV practice, Torcoli 2019): speech stays about 12 LU above
    # the music, which dips under every voice and fades in after the channel signature.
    voiced = "+".join(f"between(t,{a['at']:.2f},{a['at'] + a['seconds']:.2f})" for a in p["audio"])
    for i, clip in enumerate(clips, 1):
        path = resource(source, clip["file"], assets[clip["file"]])
        args += ["-protocol_whitelist", "file,pipe"]
        if clip.get("music"):
            args += ["-stream_loop", "-1"]
        args += ["-i", str(path)]
        delay = round(clip["at"] * 1000)
        shape = ""
        if clip.get("music"):
            if voiced:
                shape += f",volume='if({voiced},{MUSIC_UNDER_VOICE},1)':eval=frame"
            if p["effects"]:
                shape += f",afade=t=in:st=0:d={SIGNATURE_SECONDS}"
        filters.append(
            f"[{i}:a]atrim=0:{clip['seconds']},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo{shape},adelay={delay}|{delay}[a{i}]"
        )
        labels.append(f"[a{i}]")
    # Old whoosh / pop / riddle ticks: local synthesis, never under religious material.
    if p["effects"]:
        # The «روّق مع خالد» signature: three rising notes, made here, same on every clip.
        for k, (at, hz) in enumerate(SIGNATURE):
            delay = round(at * 1000)
            filters.append(
                f"sine=frequency={hz}:sample_rate=48000:duration=0.9,volume=0.22,afade=t=out:st=0.05:d=0.85:curve=exp,aformat=channel_layouts=stereo,adelay={delay}|{delay}[s{k}]"
            )
            labels.append(f"[s{k}]")
        events = []
        for s in p["scenes"][1:]:
            events.append((s["at"], 0.11, 600))
            if s.get("punch"):
                events.append((s["at"] + s["reveal_after"], 0.08, 900))
            if s.get("countdown"):
                events += [(s["at"] + s["countdown_after"] + k, 0.045, 1400) for k in range(5)]
        for k, (at, length, hz) in enumerate(events):
            delay = round(at * 1000)
            filters.append(
                f"sine=frequency={hz}:sample_rate=48000:duration={length},volume=0.13,afade=t=out:st=0:d={length},aformat=channel_layouts=stereo,adelay={delay}|{delay}[e{k}]"
            )
            labels.append(f"[e{k}]")
    mixing = (
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=longest:normalize=0,atrim=0:{seconds}"
    )
    if clips:
        # One-pass loudnorm can overshoot and AAC adds inter-sample peaks; the limiter keeps the
        # encoded true peak under the -1 dBTP gate (Khalid's PC: news peaked at -0.4 without it).
        mixing += ",loudnorm=I=-16:TP=-1.5:LRA=11,alimiter=limit=0.75:level=false"
    filters.append(mixing + "[out]")
    out = work / "mix.wav"
    run(
        [
            *args,
            "-filter_complex_threads",
            "1",
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-t",
            str(seconds),
            "-ar",
            "48000",
            "-ac",
            "2",
            str(out),
        ],
        300,
    )
    if clips:
        # One pass misses on very dynamic music (4 Nov: Snap «حدث اليوم» failed the loudness
        # gate). Measure the mix and correct it once toward -16 LUFS (owner 8 Nov 2026,
        # Snap guidance) under the same limiter.
        level = measure_audio(out, {"ffmpeg": "/usr/bin/ffmpeg", "timeout_seconds": 300}, run)
        if not -16.8 <= level["integrated_lufs"] <= -15.2 or level["true_peak_dbtp"] > -1.5:
            fixed = work / "mix-level.wav"
            gain = TARGET_LUFS - level["integrated_lufs"]
            run(
                [
                    "/usr/bin/ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(out),
                    "-af",
                    f"volume={gain:.2f}dB,alimiter=limit=0.75:level=false",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    str(fixed),
                ],
                120,
            )
            fixed.replace(out)
    return out


def qa(video: Path, p: dict[str, Any], layout: dict[str, Any], work: Path) -> dict[str, Any]:
    probe = json.loads(
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
    streams = probe["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    a = next(s for s in streams if s["codec_type"] == "audio")
    seconds = float(probe["format"]["duration"])
    if (v["width"], v["height"], v["codec_name"], v["pix_fmt"], v["r_frame_rate"]) != (
        1080,
        1920,
        "h264",
        "yuv420p",
        "30/1",
    ) or abs(seconds - p["duration"]) > 0.15:
        raise ValueError("LEGACY_QA_VIDEO_PROFILE")
    if (a["codec_name"], a["sample_rate"], a["channels"]) != ("aac", "48000", 2):
        raise ValueError("LEGACY_QA_AUDIO_PROFILE")
    low, high, largest = budget(p["app"], p["key"])
    if not low <= seconds <= high or video.stat().st_size > largest:
        raise ValueError("LEGACY_QA_LENGTH_OR_SIZE")
    if layout["overflow"] or layout["empty"] or layout["tiny"] or not layout["handle"]:
        raise ValueError("LEGACY_QA_LAYOUT")
    # Old reading agent: every visible scene (including full tafsir) must be readable.
    for s in p["scenes"]:
        text = s["body"] + s.get("head", "") + s.get("tafsir", "")
        if len(text) / s["seconds"] > 17.05 and s["role"] not in {"hook", "cta", "verse"}:
            raise ValueError("LEGACY_QA_READING_SPEED")
    profile = {"ffmpeg": "/usr/bin/ffmpeg", "timeout_seconds": 300}
    human = any(a.get("kind", "human") == "human" for a in p["audio"])
    speech = any(a.get("kind") == "speech" for a in p["audio"])
    sound = human or speech or bool(p["music"])
    audio = (
        measure_audio(video, profile, run)
        if sound
        else {"max_volume_db": measure_silence(video, profile, run)}
    )
    quiet, loud = LOUDNESS_GATE
    if sound and (not quiet <= audio["integrated_lufs"] <= loud or audio["true_peak_dbtp"] > -1):
        raise ValueError("LEGACY_QA_LOUDNESS")
    if not sound and audio["max_volume_db"] > -80:
        raise ValueError("LEGACY_QA_SACRED_NOT_SILENT")
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
        300,
    )
    # Six actual shots used by the old no-token final checklist; kept for owner review.
    shots = []
    for i, fraction in enumerate((0.08, 0.24, 0.4, 0.56, 0.72, 0.88)):
        path = work / f"shot-{i + 1}.jpg"
        run(
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-ss",
                str(seconds * fraction),
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-vf",
                "scale=540:960",
                str(path),
            ],
            30,
        )
        shots.append({"file": path.name, "sha256": sha(path.read_bytes())})
    checks = {
        "dimensions": (v["width"], v["height"]) == (1080, 1920),
        "codec": v["codec_name"] == "h264" and a["codec_name"] == "aac",
        "duration": low <= seconds <= high,
        "size": video.stat().st_size <= largest,
        "audio": (quiet <= audio["integrated_lufs"] <= loud and audio["true_peak_dbtp"] <= -1)
        if sound
        else audio["max_volume_db"] <= -80,
        "safe_window": not layout["overflow"] and not layout["tiny"],
        "content": not layout["empty"],
        "handle": bool(layout["handle"]),
        "follow_card": p["scenes"][-1]["role"] == "cta",
        "source_images": all(
            s.get("image") and s.get("image_credit")
            for s in p["scenes"]
            if s.get("role") in {"story", "headline", "point"}
        ),
    }
    score = sum(checks.values())
    if score < 10:
        raise ValueError("LEGACY_FINAL_JUDGE_REJECTED")
    return {
        "result": "PASS",
        "bytes": video.stat().st_size,
        "width": 1080,
        "height": 1920,
        "codec": "h264",
        "duration": seconds,
        "audio": "HUMAN_AND_SPEECH"
        if human and speech
        else "GENERATED_SPEECH"
        if speech
        else "HUMAN_RECORDING"
        if human
        else "LICENSED_MUSIC"
        if sound
        else "silent_sacred",
        "audio_qa": audio,
        "judge": {
            "mode": "old-no-token-checklist",
            "decision": "PASS",
            "score": score,
            "criteria": checks,
            "checks": [
                "profile",
                "audio",
                "source_images",
                "full_text",
                "safe_window",
                "handle_cta",
            ],
            "shots": shots,
        },
    }


def render(
    source: Path, destination: Path, *, history: tuple[Path, str] | None = None
) -> dict[str, Any]:
    value = json.loads((source / "content.json").read_bytes())
    p = validate(value, assets=source)
    verify_fonts(p)
    deny_ip_sockets()
    isolation = proof("/run/madar/production-inaccessible.sock")
    if destination.exists() or destination.is_symlink():
        raise ValueError("LEGACY_OUTPUT_EXISTS")
    destination.mkdir(parents=True, mode=0o750)
    errors: list[str] = []
    # The designer agent picks the look once; both attempts render the same decision.
    clips = footage.load()
    manifest = source / "background/footage.json"
    live = None
    if manifest.is_file():
        asset = footage.pinned(value, manifest)
        if asset["provider"] in {"pexels", "pixabay"}:
            live = {
                **asset,
                "title": designer.LIVE_NAMES[asset["pool"]],
            }
    if live:
        try:
            design = designer.decide(p, value, [live], history=history)
        except (OSError, ValueError, subprocess.SubprocessError):
            live = None
    if not live:
        design = designer.decide(p, value, clips, history=history)
    if live:
        design["background_source"] = {
            key: live[key] for key in ("provider", "source", "license", "credit", "sha256")
        }
    for attempt in (1, 2):
        work = destination / f"attempt-{attempt}"
        work.mkdir(mode=0o750)
        try:
            frames = work / "frames"
            (work / "design.json").write_text(json.dumps(design, ensure_ascii=False))
            run(
                [
                    os.environ.get("MADAR_NODE") or shutil.which("node") or "/usr/bin/node",
                    str(ROOT / "scripts/render_legacy_scenes.cjs"),
                    str(source / "content.json"),
                    str(source),
                    str(frames),
                    str(work / "design.json"),
                ],
                1800,
            )
            layout = json.loads((frames / "layout.json").read_bytes())
            mix = audio_mix(p, source, work)
            clip = work / "clip.mp4"
            background = {"path": design["footage_path"]}
            if p["key"] in SACRED:
                # «تلاوة» (owner 8 Nov 2026): Islamic and nature backdrops taking turns.
                montage = work / "montage.mp4"
                run(footage.montage_command(value["content_hash"], p["duration"], montage), 900)
                background = {"path": str(montage)}
                design["dim"] = min(design["dim"], 0.3)
                design["montage"] = footage.montage_order(value["content_hash"])
            run(
                [
                    "/usr/bin/ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-y",
                    "-threads",
                    "1",
                    "-framerate",
                    "15",
                    "-i",
                    str(frames / "%05d.png"),
                    "-stream_loop",
                    "-1",
                    "-i",
                    background["path"],
                    "-i",
                    str(mix),
                    "-filter_complex_threads",
                    "1",
                    "-filter_complex",
                    "[1:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                    f"drawbox=color=black@{design['dim']}:t=fill[bg];"
                    "[bg][0:v]overlay=shortest=1,format=yuv420p[v]",
                    "-map",
                    "[v]",
                    "-map",
                    "2:a",
                    "-t",
                    str(p["duration"]),
                    "-c:v",
                    "libx264",
                    "-threads:v",
                    "2",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "21",
                    "-r",
                    "30",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    str(clip),
                ],
                900,
            )
            video = work / "video.mp4"
            approved = brand_frame.load_brand()
            command = brand_frame.command(
                "/usr/bin/ffmpeg", clip, video, approved, p["duration"], True, False
            )
            for flag, setting in [
                ("-preset", "veryfast"),
                ("-crf", "21"),
                ("-maxrate", "1800k"),
                ("-bufsize", "3600k"),
            ]:
                command[command.index(flag) + 1] = setting
            command[1:1] = ["-threads", "2", "-filter_complex_threads", "1"]
            run(command, 900)
            brand = {
                "clip_box": approved["clip_box"],
                "overlay_sha256": approved["overlay"]["sha256"],
                "external_delivery": False,
            }
            report = qa(video, p, layout, work)
            inspection = designer.inspect(video, p["duration"], approved["clip_box"])
            result = {
                "schema": 1,
                "app": p["app"],
                "key": p["key"],
                "content_hash": value["content_hash"],
                "video_sha256": sha(video.read_bytes()),
                "qa": report,
                "brand": brand,
                "network_isolation": isolation,
                "attempt": attempt,
                "rebuild_reasons": errors,
                "design": {k: v for k, v in design.items() if k != "footage_path"}
                | {"inspection": inspection},
                "external_delivery": False,
            }
            (work / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            for name in ("video.mp4", "report.json", *[f"shot-{i}.jpg" for i in range(1, 7)]):
                shutil.copyfile(work / name, destination / name)
            shutil.rmtree(frames)
            return result
        except (
            ValueError,
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as error:
            errors.append(type(error).__name__ + ": " + str(error)[:160])
            shutil.rmtree(work / "frames", ignore_errors=True)
    (destination / "failed.json").write_text(json.dumps({"errors": errors}))
    raise ValueError("LEGACY_QA_FAILED_AFTER_REBUILD: " + "; ".join(errors))


if __name__ == "__main__":
    result = render(Path(sys.argv[1]), Path(sys.argv[2]))
    print(
        json.dumps(
            {"key": result["key"], "qa": result["qa"]["result"], "attempt": result["attempt"]}
        )
    )
