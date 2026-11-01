"""Frozen live headlines rendered in an isolated oneshot, then sent once by the listener."""

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from studio_core.delivery.evidence import multipart
from studio_core.renderer import write_once
from studio_core.sources.news import validate

from . import command_cards
from .server_preview import STATE, safe_path

ROOT = STATE / "news-clips"


def labels(content: dict[str, Any]) -> list[tuple[str, int, int]]:
    parts = command_cards.lines(content["headline"])
    if len(parts) > 9:
        raise ValueError("Headline exceeds card")
    return [
        (content["title"], 52, 430),
        *[(line, 46, 600 + i * 75) for i, line in enumerate(parts)],
        ("المصدر: " + content["source"], 34, 1340),
        (content["published_label"], 26, 1430),
    ]


def render_event(event: str, root: Path = ROOT) -> None:
    if not re.fullmatch("[0-9a-f]{64}", event):
        raise ValueError("Invalid news clip event")
    directory = safe_path(root / event)
    content = json.loads(safe_path(directory / "content.json").read_bytes())
    validate(content, time.time())
    result = command_cards.render(labels(content), directory / "render")
    validate(content, time.time())
    result["content_hash"] = content["content_hash"]
    write_once(directory / "result.json", json.dumps(result, ensure_ascii=False).encode())


def deliver(
    content: dict[str, Any],
    event: str,
    token: str,
    owner: str,
    transport: Any,
    runtime: Any,
    halt: Path,
    *,
    root: Path = ROOT,
    start: Any = None,
) -> None:
    validate(content, time.time())
    if halt.exists():
        raise ValueError("News send halted")
    tag = hashlib.sha256(event.encode()).hexdigest()
    directory = safe_path(root / tag)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_once(directory / "content.json", json.dumps(content, ensure_ascii=False).encode())
    unit = "madar-v3-snap-news-clip@" + tag + ".service"
    if start is None:
        subprocess.run(["/usr/bin/systemctl", "start", unit], check=True, timeout=300)  # noqa: S603
    else:
        start(unit)
    result = json.loads(safe_path(directory / "result.json").read_bytes())
    video = safe_path(directory / "render/video.mp4").read_bytes()
    validate(content, time.time())
    if (
        halt.exists()
        or result.get("content_hash") != content["content_hash"]
        or result.get("video_sha256") != hashlib.sha256(video).hexdigest()
        or not 0 < len(video) <= 50_000_000
        or result.get("safe_area", {}).get("result") != "PASS"
        or result.get("network_isolation")
        != {"ipv4": True, "ipv6": True, "udp": True, "egress_unreachable": True}
    ):
        raise ValueError("News clip admission failed")
    body, kind = multipart(owner, video, "", "news.mp4")
    response = transport(token, "sendVideo", body, kind)
    if (
        not isinstance(response, dict)
        or type(response.get("message_id")) is not int
        or response["message_id"] <= 0
        or str(response.get("chat", {}).get("id")) != owner
    ):
        raise ValueError("News send unconfirmed")
    with runtime.locked():
        runtime.append(
            "NEWS_ACK",
            command=content["command"],
            video_sha=result["video_sha256"],
            content_hash=content["content_hash"],
            message_id=response["message_id"],
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("event")
    args = parser.parse_args()
    render_event(args.event)


if __name__ == "__main__":
    main()
