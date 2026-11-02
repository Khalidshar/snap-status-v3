"""One requested daily brief: civil dates, approved Quran and two original news headlines."""

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from studio_core.renderer import write_once
from studio_core.sources.daily_brief import (
    dates as dates,
)
from studio_core.sources.daily_brief import (
    prepare as prepare,
)
from studio_core.sources.daily_brief import (
    validate_brief as validate_brief,
)
from studio_core.sources.live import LiveNews

from .server_preview import STATE, safe_path

ROOT = STATE / "briefs"


def render_event(event: str, root: Path = ROOT) -> None:
    if not re.fullmatch("[0-9a-f]{64}", event):
        raise ValueError("Invalid brief event")
    directory = safe_path(root / event)
    value = json.loads(safe_path(directory / "content.json").read_bytes())
    validate_brief(value, time.time())
    from studio_core.presentation.daily_brief import render

    render(value, directory, app="snap-bot-v2")


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
