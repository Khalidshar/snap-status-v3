"""App-owned live-news review layout. No delivery or listener changes."""

import textwrap
import tomllib
from pathlib import Path
from typing import Any

from studio_core.sources.preview import render as render_review

ROOT = Path(__file__).resolve().parents[3]
APP = "status-bot"


def labels(content: dict[str, Any]) -> list[tuple[str, int, int]]:
    title = content["headline"]
    if any(len(word) > 28 for word in title.split()):
        raise ValueError("Headline word exceeds card width")
    lines = textwrap.wrap(title, width=28, break_long_words=False, break_on_hyphens=False)
    if len(lines) > 8:
        raise ValueError("Headline exceeds card height")
    return [
        (content["title"], 52, 420),
        *[(line, 56, 620 + index * 80) for index, line in enumerate(lines)],
        ("المصدر: " + content["source"], 38, 1320),
        (content["published_label"], 26, 1410),
        ("معاينة للمراجعة", 32, 1540),
    ]


def render(input_file: Path, output: Path) -> dict[str, Any]:
    profile = tomllib.loads((ROOT / "config/status/weather.toml").read_text())["slice"]["profile"]
    settings = {
        "app": APP,
        "egress_socket": "/run/madar-status-news/egress.sock",
        "ffmpeg": profile["ffmpeg"],
        "ffprobe": profile["ffprobe"],
        "ffmpeg_version": profile["ffmpeg_version"],
        "fc_query": profile["fc_query"],
        "font": str(ROOT / "assets/fonts/arabeyes/ae_AlMohanad.ttf"),
        "font_sha256": "3e430d92eafb853d609ceaf36ddf243104472940792e1a95cd6314b9ceb52923",
        "overlay": str(ROOT / "assets/brand/rawwag/frame_personal.png"),
        "overlay_sha256": "58e849c97d60750b7af86df1e323f8bf72ce4383f18ef3f3f89ddc779c42f2b4",
    }
    return render_review(APP, input_file, output, settings, labels)
