"""Local FFmpeg renderer. Run only in the no-network systemd unit."""

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from studio_core.config import Config
from studio_core.jobs import canonical

# Shared with the Status application through the core; re-exported for existing callers.
from studio_core.renderer import (
    attempt_workspace,
    ensure_glyphs,
    proof,
    publish_once,
    run,
)
from studio_core.renderer import (
    cleanup_attempt_temps as cleanup_attempt_temps,
)
from studio_core.renderer import (
    file_sha256 as file_sha256,
)
from studio_core.renderer import (
    sync_dir as sync_dir,
)
from studio_core.renderer import (
    verify_once as verify_once,
)
from studio_core.renderer import (
    write_once as write_once,
)

from . import brand_frame, sacred_cards
from .did_you_know import is_sacred, validate_content
from .fact_audio import ambient_source, measure_audio
from .fact_layout import (
    SACRED_BODY,
    card_filters,
    display_text,
    fact_labels,
    label_y,
    measure_label,
    sacred_body_box,
    sacred_display_body,
    sacred_labels,
    stack_sacred,
)
from .fact_motion import background_filters, glow_filter, label_filter, reveal_windows
from .font_admission import admit_font, choose_font, fallback_fonts, font_paths
from .weather import content_hash as calculate_content_hash

ALLOWED_CONTENT = {"weather", "did_you_know"}
# fact_labels() draws the identity (name, handle) first; card_filters() ends with the border.
FRAMED_SKIP_LABELS = 2
CARD_BORDER = "drawbox=x=80:y=340:w=920:h=1240:color=0xD6B26C:t=2"


def video_filename(content_type: object) -> str:
    if content_type not in ALLOWED_CONTENT:
        raise ValueError("Unsupported render content type")
    return "weather.mp4" if content_type == "weather" else "did-you-know.mp4"


def sacred_body_image(
    body: str, box: tuple[int, int, int, int], profile: dict[str, Any], design: Path
) -> tuple[Path, tuple[int, int]]:
    """The verbatim body, shaped whole by pango (drawtext misplaces vowel marks)."""
    from . import sacred_text

    font = admit_font(profile["sacred_font"], profile["sacred_font_sha256"], profile)
    version = run([profile["pango_view"], "--version"], 5).stdout.strip()
    if version != profile["pango_version"]:
        raise ValueError("pango-view version mismatch")
    _, _, width, height = box
    image = design / "body.png"
    size = sacred_text.render_text_png(
        body,
        font,
        image,
        pango_view=profile["pango_view"],
        width_px=width,
        font_size=profile.get("sacred_font_size", 52),
        color="#F6EEDE",
        max_height_px=height,
    )
    if not 0 < size[0] <= width or not 0 < size[1] <= height:
        raise ValueError("Sacred text exceeds its card")
    return image, size


def sacred_body(
    image: Path,
    size: tuple[int, int],
    y: int,
    profile: dict[str, Any],
    card: str,
    window: tuple[float, float],
    safe_boxes: list[dict[str, int]],
) -> str:
    """Overlay the body image at its stacked row, fading in on its reveal window."""
    left, _, width, _ = SACRED_BODY
    safe_boxes.append(
        {"x": left + (width - size[0]) // 2, "y": y, "width": size[0], "height": size[1]}
    )
    start, end = window
    fps = profile["fps"]
    return (
        f"movie={image},format=rgba,loop=loop=-1:size=1:start=0,setpts=N/({fps}*TB),"
        f"fade=t=in:st={start:g}:d={end - start:g}:alpha=1[body];"
        f"[in]{card}[card];[card][body]overlay=x=(W-w)/2:y={y}:format=auto[out]"
    )


def sacred_sequence(
    images: list[tuple[Path, tuple[int, int]]],
    y: int,
    height: int,
    seconds: float,
    profile: dict[str, Any],
    card: str,
    window: tuple[float, float],
    safe_boxes: list[dict[str, int]],
) -> str:
    """Consecutive full pages, with no overlap, crop, stretching or omitted text."""
    script = f"[in]{card}[pagecard0];"
    for index, (image, size) in enumerate(images):
        top = y + (height - size[1]) // 2
        safe_boxes.append(
            {"x": 130 + (820 - size[0]) // 2, "y": top, "width": size[0], "height": size[1]}
        )
        fade = (
            f",fade=t=in:st={window[0]:g}:d={window[1] - window[0]:g}:alpha=1" if index == 0 else ""
        )
        output = "out" if index == len(images) - 1 else f"pagecard{index + 1}"
        script += (
            f"movie={image},format=rgba,loop=loop=-1:size=1:start=0,"
            f"setpts=N/({profile['fps']}*TB){fade}[page{index}];"
            f"[pagecard{index}][page{index}]overlay=x=(W-w)/2:y={top}:format=auto:"
            f"enable='gte(t,{index * seconds:g})*lt(t,{(index + 1) * seconds:g})'[{output}];"
        )
    return script.rstrip(";")


def render(
    config: Config, settings: dict[str, Any], job_id: str, content_hash: str, attempt: int
) -> dict[str, Any]:
    # DB-free renderer. Ownership and DB adoption belong to the worker.
    if not re.fullmatch("[0-9a-f]{32}", job_id) or type(attempt) is not int or attempt < 1:
        raise ValueError("Invalid render identity")
    spool = Path(settings["spool_root"])
    input_root = Path(settings["input_root"])
    if (
        not spool.is_absolute()
        or spool.name != "renders"
        or not input_root.is_absolute()
        or input_root.name != "renders"
    ):
        raise ValueError("Invalid render roots")
    content_path = input_root / job_id / "attempts" / str(attempt) / "input/content.json"
    for target in (spool, content_path):
        cursor = Path(target.anchor)
        for part in target.parts[1:]:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("Unsafe render path")
    fd = os.open(content_path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        import stat

        meta = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != settings["worker_uid"]
            or meta.st_nlink != 1
            or meta.st_mode & 0o037
            or not 0 < meta.st_size <= 16384
        ):
            raise ValueError("Unsafe content file")
        content = json.loads(source.read(16385))
    digest = calculate_content_hash(content)
    if (
        digest != content_hash
        or content.get("content_hash") != digest
        or content.get("type") not in ALLOWED_CONTENT
        # Facts are checked by validate_content below: sacred only for a sacred kind.
        or (content.get("type") != "did_you_know" and content.get("sacred") is not False)
    ):
        raise ValueError("Unverified render input")
    is_fact = content["type"] == "did_you_know"
    if is_fact:
        validate_content(content)
        profile = settings["profile"]
        if (profile["width"], profile["height"]) != (1080, 1920) or not 8 <= profile[
            "duration_seconds"
        ] <= 12:
            raise ValueError("Invalid fact video profile")
    # The approved "روّق مع خالد" frame wraps every fact video when its profile names it.
    frame_name = settings["profile"].get("brand_frame") if is_fact else None
    if frame_name not in (None, "rawwag"):
        raise ValueError("Unknown brand frame")
    brand = brand_frame.load_brand() if frame_name else None
    isolation = proof(settings["egress_socket"])
    spool.mkdir(parents=True, exist_ok=True, mode=0o750)
    root = spool / job_id
    if root.is_symlink():
        raise ValueError("Unsafe job directory")
    root.mkdir(exist_ok=True, mode=0o750)
    work = attempt_workspace(root, attempt)
    run_id = uuid.uuid4().hex
    if content["expires_at"] <= time.time():
        raise ValueError("Expired content")
    profile = settings["profile"]
    font, latin_font = font_paths(profile)
    fallbacks = fallback_fonts(profile)
    charsets = {
        path: run([profile["fc_query"], "--format=%{charset}", str(path)], 5).stdout
        for path in dict.fromkeys([font, latin_font, *fallbacks])
    }
    version = run([profile["ffmpeg"], "-version"], 5).stdout.splitlines()[0]
    if version != profile["ffmpeg_version"]:
        raise ValueError("FFmpeg version mismatch")
    write_once(
        work / "admission.json",
        json.dumps(
            {
                "app": config.app,
                "job_id": job_id,
                "attempt": attempt,
                "content_hash": content_hash,
                "profile_sha256": hashlib.sha256(canonical(profile).encode()).hexdigest(),
            },
            sort_keys=True,
        ).encode(),
    )
    labels: list[tuple[Any, ...]] = (
        []
        if is_fact
        else [
            ("روق مع خالد", 50, 210, "0xD6B26C"),
            ("@rawwag_khalid", 38, 290, "0xD6B26C"),
            (content["title"], 108, 440, "0xD6B26C"),
            (f"{content['temperature_c']:.1f}°", 166, 720, "0xD6B26C"),
            ("درجة مئوية", 62, 940, "0xF6EEDE"),
            (f"الرياح {content['wind_kmh']:.1f} كم في الساعة", 60, 1080, "0xF6EEDE"),
            ("تقدير نموذجي للطقس", 44, 1300, "0xF6EEDE"),
            (content["model_time"][:16].replace("T", " "), 36, 1380, "0xF6EEDE"),
            (content["attribution"], 30, 1490, "0xF6EEDE"),
        ]
    )
    sacred = is_fact and is_sacred(content)
    body_window = (0.0, 0.0)
    if sacred:
        labels = sacred_labels(content, profile)
        # The body image takes the last body slot of the reveal, after the Prophet ﷺ line.
        windows = reveal_windows(len(labels) + 1, profile["duration_seconds"])
        body_window = windows.pop(len(labels) - 2)
    elif is_fact:
        labels = fact_labels(content, profile)
    if is_fact and not sacred:
        windows = reveal_windows(len(labels), profile["duration_seconds"])
    elif not is_fact:
        windows = []
    design = work / "design" / run_id
    design.mkdir(mode=0o750)
    if sacred:
        if content.get("kind") in sacred_cards.KINDS:
            if (
                run([profile["pango_view"], "--version"], 5).stdout.strip()
                != profile["pango_version"]
            ):
                raise ValueError("pango-view version mismatch")
            page_parts, page_images = sacred_cards.images(content, profile, design)
            page_seconds = profile["duration_seconds"]
            page_height = max(size[1] for _, size in page_images)
            labels, body_top = stack_sacred(labels, page_height)
            profile = {**profile, "duration_seconds": page_seconds * len(page_images)}
        else:
            body_image, body_size = sacred_body_image(
                sacred_display_body(content), sacred_body_box(content), profile, design
            )
            labels, body_top = stack_sacred(labels, body_size[1])
    safe_boxes = []
    filters = card_filters(profile["fps"])
    if is_fact:
        filters = background_filters(filters)
        filters.append(glow_filter(profile["fps"], profile["duration_seconds"]))
    # Inside the frame the frame carries the identity and the gold border: the card
    # draws neither (no second name/handle, no third gold box). Timings stay put.
    framed_skip = FRAMED_SKIP_LABELS if brand else 0
    if brand:
        if labels[1][0] != "@rawwag_khalid" or "خالد" not in labels[0][0]:
            raise ValueError("Unexpected fact identity labels")
        filters.remove(CARD_BORDER)
    y: int | str
    label_font: Path
    for index, label in enumerate(labels):
        if index < framed_skip:
            continue
        text, size, y, color = label[:4]
        if len(text) > 110 or any(ord(c) < 32 for c in text):
            raise ValueError("Unsafe label")
        label_font = (
            {"arabic": font, "latin": latin_font}[label[4]]
            if is_fact
            else latin_font
            if index in (1, 3, 7, 8)
            else font
        )
        label_font = choose_font(text, label_font, charsets, fallbacks, ensure_glyphs)
        text = display_text(
            text, label[4] if is_fact else "latin" if index in (1, 3, 7, 8) else "arabic"
        )
        path = design / f"text{index}.txt"
        write_once(path, text.encode("utf-8"))
        x = "(w-text_w)/2"
        y = label_y(label) if is_fact else y
        filter_value = f"drawtext=fontfile={label_font}:textfile={path}:expansion=none:text_shaping=1:fontsize={size}:fontcolor={color}:x={x}:y={y}"
        if is_fact:
            try:
                safe_boxes.append(measure_label(profile, filter_value))
            except ValueError as error:
                raise ValueError(f"{error}; label={text!r}; size={size}") from error
            filter_value = label_filter(filter_value, windows[index])
        filters.append(filter_value)
    script = ",".join(filters)
    if sacred:
        if content.get("kind") in sacred_cards.KINDS:
            script = sacred_sequence(
                page_images,
                body_top,
                page_height,
                page_seconds,
                profile,
                script,
                body_window,
                safe_boxes,
            )
        else:
            script = sacred_body(
                body_image, body_size, body_top, profile, script, body_window, safe_boxes
            )
    filter_path = design / "filters.txt"
    write_once(filter_path, script.encode())
    output = work / "output" / video_filename(content["type"])
    partial = work / "temp" / (run_id + ".partial.mp4")
    args = [
        profile["ffmpeg"],
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-threads",
        "1",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x0B3D2E:s={profile['width']}x{profile['height']}:r={profile['fps']}:d={profile['duration_seconds']}",
        *(
            [
                "-f",
                "lavfi",
                "-i",
                ambient_source(
                    profile["duration_seconds"],
                    page_count=len(page_images) if content.get("kind") in sacred_cards.KINDS else 1,
                ),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-b:a",
                "128k",
                "-threads:a",
                "1",
            ]
            if is_fact
            else ["-an"]
        ),
        "-filter_script:v",
        str(filter_path),
        "-t",
        str(profile["duration_seconds"]),
        "-c:v",
        "libx264",
        "-threads:v",
        "1",
        "-preset",
        profile["preset"],
        "-crf",
        str(min(profile["crf"], 18) if is_fact else profile["crf"]),
        *(["-tune", "grain"] if is_fact else []),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(partial),
    ]
    if output.exists():
        # Explicit same-attempt retry uses already published bytes, not another encode.
        qa = validate(
            output,
            profile,
            fact=is_fact,
            page_count=len(page_images) if content.get("kind") in sacred_cards.KINDS else 1,
        )
    else:
        if brand:
            # QA, the hash and preview.png all describe the framed video the owner reviews.
            clip = work / "temp" / (run_id + ".clip.mp4")
            args[-1] = str(clip)
            run(args, profile["timeout_seconds"])
            brand_frame.compose(
                clip,
                partial,
                ffmpeg=profile["ffmpeg"],
                ffprobe=profile["ffprobe"],
                timeout=profile["timeout_seconds"],
                normalize_audio=False,
            )
        else:
            run(args, profile["timeout_seconds"])
        qa = validate(
            partial,
            profile,
            fact=is_fact,
            page_count=len(page_images) if content.get("kind") in sacred_cards.KINDS else 1,
        )
        publish_once(partial, output)
    digest = file_sha256(output)
    preview = work / "output/preview.png"
    preview_partial = work / "temp" / (run_id + ".preview.png")
    if preview.exists():
        file_sha256(preview)
    else:
        run(
            [
                profile["ffmpeg"],
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-ss",
                # Facts: the first frame of the reading hold, when every line is on screen.
                f"{max(end for _, end in windows) + 0.3:g}" if is_fact else "1",
                "-i",
                str(output),
                "-frames:v",
                "1",
                str(preview_partial),
            ],
            30,
        )
        publish_once(preview_partial, preview)
    artifact = output.relative_to(spool.parent).as_posix()
    result = {
        "job_id": job_id,
        "content_hash": content_hash,
        "video_sha256": digest,
        "attempt": attempt,
        "artifact": artifact,
        "qa": qa,
        "network_isolation": isolation,
        "tool_version": version,
        "font_sha256": profile["font_sha256"],
        "latin_font_sha256": profile["latin_font_sha256"],
        "glyph_coverage": "PASS",
    }
    if content.get("kind") in sacred_cards.KINDS:
        result["sacred_pagination"] = sacred_cards.evidence(content, page_parts, page_seconds)
    if is_fact:
        if brand:
            safe_boxes = [brand_frame.framed_box(box, brand["clip_box"]) for box in safe_boxes]
            result["brand_frame"] = {
                "name": frame_name,
                "overlay_sha256": brand["overlay"]["sha256"],
                "clip_box": brand["clip_box"],
            }
        result.update(
            {"content_type": "did_you_know", "safe_area": {"result": "PASS", "boxes": safe_boxes}}
        )
    write_once(
        work / "output/render-result.json",
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2).encode(),
    )
    return result


def validate(
    path: Path, profile: dict[str, Any], *, fact: bool = False, page_count: int = 1
) -> dict[str, Any]:
    value = json.loads(
        run(
            [
                profile["ffprobe"],
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            30,
        ).stdout
    )
    videos = [s for s in value["streams"] if s["codec_type"] == "video"]
    audios = [s for s in value["streams"] if s["codec_type"] == "audio"]
    if not fact and audios:
        raise ValueError("Audio is not enabled for weather")
    if len(videos) != 1:
        raise ValueError("Video stream missing")
    stream = videos[0]
    duration = float(value["format"]["duration"])
    if (
        type(page_count) is not int
        or not 1 <= page_count <= sacred_cards.MAX_PAGES
        or (not fact and page_count != 1)
    ):
        raise ValueError("Invalid page count")
    if fact and duration > 12 * page_count:
        raise ValueError("Fact video exceeds duration limit")
    if (
        stream["width"] != profile["width"]
        or stream["height"] != profile["height"]
        or stream["codec_name"] != "h264"
        or stream["pix_fmt"] != "yuv420p"
    ):
        raise ValueError("Invalid video profile")
    if (
        abs(duration - profile["duration_seconds"]) > 0.25
        or not 0 < path.stat().st_size <= profile["max_bytes"]
    ):
        raise ValueError("Invalid duration/size")
    audio_qa = {}
    if fact:
        if len(audios) != 1:
            raise ValueError("Fact requires one ambient audio stream")
        audio = audios[0]
        if (
            audio.get("codec_name"),
            audio.get("sample_rate"),
            audio.get("channels"),
            audio.get("channel_layout"),
        ) != ("aac", "48000", 2, "stereo"):
            raise ValueError("Invalid ambient audio profile")
        audio_duration = float(audio.get("duration", "nan"))
        if not abs(audio_duration - float(stream.get("duration", duration))) <= 0.1:
            raise ValueError("Ambient audio duration differs from video")
        audio_qa = measure_audio(path, profile, run)
        if not -19 <= audio_qa["integrated_lufs"] <= -17 or audio_qa["true_peak_dbtp"] > -1.5:
            raise ValueError("Ambient audio loudness/peak exceeds limits")
        audio_qa["phone_lufs"] = measure_audio(path, profile, run, phone=True)["integrated_lufs"]
        if audio_qa["phone_lufs"] < -24:
            raise ValueError("Ambient audio too quiet after phone simulation")
    run(
        [
            profile["ffmpeg"],
            "-v",
            "error",
            "-xerror",
            "-nostdin",
            "-i",
            str(path),
            "-f",
            "null",
            "-",
        ],
        profile["timeout_seconds"],
    )
    return {
        "result": "PASS",
        "width": stream["width"],
        "height": stream["height"],
        "duration": duration,
        "bytes": path.stat().st_size,
        "codec": "h264",
        "audio": "generated_ambient" if fact else "not_required_for_weather_card",
        **({"audio_qa": audio_qa} if fact else {}),
        "visual_review": "PENDING",
    }
