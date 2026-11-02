"""One requested daily brief: civil dates, approved Quran and two original news headlines."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from studio_core.config import Config
from studio_core.network import NetworkClient
from studio_core.renderer import run, write_once
from studio_core.sources.live import LiveNews
from studio_core.sources.news import DIGITS, collect, digest, published_label, validate

from . import civil_calendar, command_cards
from .did_you_know import load_kind, make_content, validate_content
from .render import render as render_faith
from .server_preview import STATE, safe_path

ROOT = STATE / "briefs"


def dates(now: float) -> tuple[str, str]:
    day = datetime.fromtimestamp(now, ZoneInfo("Asia/Riyadh"))
    year, month, hijri_day = civil_calendar.hijri(day.date())
    return (
        published_label(now),
        f"{hijri_day} {civil_calendar.MONTHS[month - 1]} {year} هـ".translate(DIGITS),
    )


def validate_brief(value: dict[str, Any], now: float) -> None:
    if value.get("schema") != 1 or value.get("content_hash") != digest(value):
        raise ValueError("Brief snapshot changed")
    if (
        value.get("dates") != list(dates(value["at"]))
        or not value["at"] <= now < value["expires_at"]
    ):
        raise ValueError("Brief dates or expiry changed")
    validate_content(value["faith"])
    if value["faith"].get("kind") != "verse":
        raise ValueError("Brief faith kind changed")
    for key in ("saudi", "tech"):
        validate(value[key], now)
        if value[key]["command"] != key:
            raise ValueError("Brief news category changed")
    if value["expires_at"] != min(value[k]["expires_at"] for k in ("faith", "saudi", "tech")):
        raise ValueError("Brief expiry changed")


def prepare(service: LiveNews, event: str, *, client: Any = None) -> dict[str, Any] | None:
    if not isinstance(event, str) or not 1 <= len(event) <= 100:
        raise ValueError("Invalid brief event")
    tag, now = "brief:" + event, time.time()
    with service.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM replies WHERE event=?", (tag,)).fetchone():
            return None
        if db.execute("SELECT count(*) FROM replies WHERE at>?", (now - 60,)).fetchone()[0] >= 4:
            raise ValueError("Brief rate limit")
        seen = frozenset(
            r[0]
            for r in db.execute(
                "SELECT fingerprint FROM replies WHERE fingerprint IS NOT NULL AND at>?",
                (now - 30 * 86400,),
            )
        )
        db.execute("INSERT INTO replies VALUES(?,NULL,'WORKING',?)", (tag, now))
    try:
        client = client or NetworkClient(service.config)
        news = {k: collect(k, client, now, seen=seen)["content"] for k in ("saudi", "tech")}
        facts = load_kind("verse")
        day = datetime.fromtimestamp(now, ZoneInfo("Asia/Riyadh")).date()
        faith = make_content(facts[day.toordinal() % len(facts)], now, "verse")
        value = {
            "schema": 1,
            "at": now,
            "dates": list(dates(now)),
            "faith": faith,
            **news,
            "expires_at": min(faith["expires_at"], *(c["expires_at"] for c in news.values())),
        }
        value["content_hash"] = digest(value)
        validate_brief(value, time.time())
        marks = [news[k]["fingerprint"] for k in ("saudi", "tech")]
        if len(set(marks)) != 2:
            raise ValueError("Brief headlines duplicate")
        with service.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key, mark in zip(("saudi", "tech"), marks, strict=True):
                if db.execute(
                    "SELECT 1 FROM replies WHERE fingerprint=? AND at>?", (mark, now - 30 * 86400)
                ).fetchone():
                    raise ValueError("Brief headline already reserved")
                db.execute(
                    "INSERT INTO replies VALUES(?,?,'SEND_INTENT',?)", (tag + ":" + key, mark, now)
                )
            db.execute("UPDATE replies SET state='SEND_INTENT' WHERE event=?", (tag,))
        return value
    except Exception:
        with service.connect() as db:
            db.execute("UPDATE replies SET state='FETCH_FAILED' WHERE event=?", (tag,))
        raise


def render_event(event: str, root: Path = ROOT) -> None:
    if not re.fullmatch("[0-9a-f]{64}", event):
        raise ValueError("Invalid brief event")
    directory = safe_path(root / event)
    value = json.loads(safe_path(directory / "content.json").read_bytes())
    validate_brief(value, time.time())
    outputs = []
    card = command_cards.render(
        [("يومك في دقيقة", 52, 430), (value["dates"][0], 30, 740), (value["dates"][1], 44, 940)],
        directory / "dates",
    )
    outputs.append(Path(card["output"]))
    job = uuid.uuid4().hex
    input_root = directory / "input/renders"
    source = input_root / job / "attempts/1/input/content.json"
    source.parent.mkdir(parents=True, mode=0o700)
    write_once(source, json.dumps(value["faith"], ensure_ascii=False).encode())
    os.chmod(source, 0o640)
    settings: dict[str, Any] = {
        "spool_root": str(directory / "faith/renders"),
        "input_root": str(input_root),
        "worker_uid": os.getuid(),
        "profile": command_cards.profile(),
        "egress_socket": "/run/madar-brief-no-egress.sock",
    }
    faith = render_faith(
        Config.load(command_cards.ROOT / "config/snap/did_you_know.toml"),
        settings,
        job,
        value["faith"]["content_hash"],
        1,
    )
    outputs.append(Path(settings["spool_root"]).parent / faith["artifact"])
    for key in ("saudi", "tech"):
        content = value[key]
        parts = command_cards.lines(content["headline"])
        if len(parts) > 9:
            raise ValueError("Brief headline exceeds card")
        card = command_cards.render(
            [
                (content["title"], 52, 430),
                *[(s, 46, 600 + i * 75) for i, s in enumerate(parts)],
                ("المصدر: " + content["source"], 34, 1340),
                (content["published_label"], 26, 1430),
            ],
            directory / key,
        )
        outputs.append(Path(card["output"]))
    listing = directory / "concat.txt"
    write_once(listing, "".join(f"file '{p}'\n" for p in outputs).encode())
    p = command_cards.profile()
    video = directory / "video.mp4"
    run(
        [
            p["ffmpeg"],
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(video),
        ],
        180,
    )
    qa = json.loads(
        run(
            [
                p["ffprobe"],
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(video),
            ],
            30,
        ).stdout
    )
    videos = [s for s in qa["streams"] if s.get("codec_type") == "video"]
    audios = [s for s in qa["streams"] if s.get("codec_type") == "audio"]
    if (
        len(videos) != 1
        or len(audios) != 1
        or (videos[0]["width"], videos[0]["height"], videos[0]["codec_name"], videos[0]["pix_fmt"])
        != (1080, 1920, "h264", "yuv420p")
        or (audios[0]["codec_name"], audios[0]["sample_rate"]) != ("aac", "48000")
    ):
        raise ValueError("Brief media profile changed")
    run(
        [p["ffmpeg"], "-v", "error", "-xerror", "-nostdin", "-i", str(video), "-f", "null", "-"],
        180,
    )
    duration = float(qa["format"]["duration"])
    if not 30 <= duration <= 60 or not 0 < video.stat().st_size <= 50_000_000:
        raise ValueError("Brief exceeds one minute or size limit")
    validate_brief(value, time.time())
    write_once(
        directory / "result.json",
        json.dumps(
            {
                "content_hash": value["content_hash"],
                "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
                "duration": duration,
            }
        ).encode(),
    )


def reply(
    service: LiveNews,
    event: str,
    send_text: Any,
    send_video: Any,
    runtime: Any,
    halt: Path,
    *,
    root: Path = ROOT,
    client: Any = None,
    start: Any = None,
) -> None:
    if halt.exists():
        send_text("الإرسال موقوف، اكتب «شغّل» لإعادته")
        return
    from studio_core.sources.news import NewsUnavailable

    try:
        value = prepare(service, event, client=client)
    except NewsUnavailable:
        send_text("لا يوجد خبر سعودي وخبر تقنية حديثان لإكمال يومك في دقيقة الآن.")
        return
    if value is None:
        return
    tag = hashlib.sha256(event.encode()).hexdigest()
    directory = safe_path(root / tag)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_once(directory / "content.json", json.dumps(value, ensure_ascii=False).encode())
    state = "UNKNOWN"
    try:
        unit = "madar-v3-snap-brief@" + tag + ".service"
        if start is None:
            subprocess.run(["/usr/bin/systemctl", "start", unit], check=True, timeout=600)  # noqa: S603
        else:
            start(unit)
        result = json.loads(safe_path(directory / "result.json").read_bytes())
        raw = safe_path(directory / "video.mp4").read_bytes()
        validate_brief(value, time.time())
        if (
            halt.exists()
            or not 0 < len(raw) <= 50_000_000
            or result["content_hash"] != value["content_hash"]
            or result["video_sha256"] != hashlib.sha256(raw).hexdigest()
            or not 30 <= result["duration"] <= 60
        ):
            raise ValueError("Brief admission failed")
        send_video(raw)
        with runtime.locked():
            runtime.append(
                "BRIEF_ACK", video_sha=result["video_sha256"], content_hash=value["content_hash"]
            )
        state = "ACK"
    except Exception:
        state = "UNKNOWN"
        raise
    finally:
        with service.connect() as db:
            db.execute(
                "UPDATE replies SET state=? WHERE event IN (?,?,?)",
                (state, "brief:" + event, "brief:" + event + ":saudi", "brief:" + event + ":tech"),
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("event")
    render_event(parser.parse_args().event)


if __name__ == "__main__":
    main()
