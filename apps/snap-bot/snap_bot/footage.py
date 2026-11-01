"""Licensed moving backdrops shown inside the approved frame's window (owner decision 2026-11-01).

The renderer has no network, so it only reads clips pinned here by fingerprint. Live
providers (Pexels, Pixabay, Coverr) feed the same contract later; Mixkit clips are the
keyless pool every render can rely on.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from .sacred_cards import KINDS as QURAN_KINDS

ROOT = Path(__file__).resolve().parents[3] / "assets/footage"
SACRED = {"hadith", "dua", "fast", "adhkar_am", "adhkar_pm", *QURAN_KINDS}
POOLS = ("calm", "night", "day")
LICENSES = {"Mixkit": "Mixkit Stock Video Free License"}


def load(root: Path = ROOT) -> list[dict[str, Any]]:
    clips = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["clips"]
    for clip in clips:
        path = root / clip["file"]
        if (
            clip["pool"] not in POOLS
            or LICENSES.get(clip["provider"]) != clip["license"]
            or path.is_symlink()
            or path.parent != root
            or hashlib.sha256(path.read_bytes()).hexdigest() != clip["sha256"]
        ):
            raise ValueError("Footage fingerprint or license changed")
    return clips


def pool(kind: object) -> str:
    if kind in SACRED:
        return "calm"
    return "night" if kind == "night" else "day"


def choose(content: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    """Same content, same backdrop: a retry never changes the picture behind the text."""
    clips = [c for c in load(root) if c["pool"] == pool(content.get("kind"))]
    if not clips:
        raise ValueError("No footage for this kind")
    index = int(hashlib.sha256(content["body"].encode()).hexdigest()[:8], 16) % len(clips)
    return {**clips[index], "path": str(root / clips[index]["file"])}


def filters(fps: int) -> list[str]:
    # Fill the 9:16 canvas, then dim it so cream and gold text stay readable.
    if type(fps) is not int or not 1 <= fps <= 60:
        raise ValueError("Invalid footage frame rate")
    return [
        "scale=1080:1920:force_original_aspect_ratio=increase",
        "crop=1080:1920",
        f"fps={fps}",
        "eq=brightness=-0.10:saturation=0.85",
        "drawbox=x=0:y=0:w=iw:h=ih:color=black@0.35:t=fill",
        "format=yuv420p",
    ]
