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

ROOT = Path(__file__).resolve().parents[4] / "assets/footage"
SACRED = {"hadith", "dua", "fast", "adhkar_am", "adhkar_pm", *QURAN_KINDS}
POOLS = ("calm", "night", "day")
LICENSES = {"Mixkit": "Mixkit Stock Video Free License"}


def load(root: Path = ROOT) -> list[dict[str, Any]]:
    clips: list[dict[str, Any]] = json.loads((root / "manifest.json").read_text(encoding="utf-8"))[
        "clips"
    ]
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
        "eq=brightness=-0.14:saturation=0.80",
        "drawbox=x=0:y=0:w=iw:h=ih:color=black@0.50:t=fill",
        "format=yuv420p",
    ]


def prepare(
    content: dict[str, Any],
    directory: Path,
    socket: Path,
    *,
    providers: tuple[str, ...] | None = None,
) -> Path:
    """Worker-side prefetch. Renderer only consumes this pinned local manifest."""
    from studio_core.network.client import NetworkError

    from .video_backgrounds import prefetch, verify

    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    manifest = directory / "footage.json"
    if manifest.exists():
        pinned(content, manifest)
        return manifest
    try:
        fetched = prefetch(
            "calm nature landscape" if pool(content.get("kind")) == "calm" else "nature landscape",
            directory,
            socket,
            **({"providers": providers} if providers is not None else {}),
        )
        local = verify(fetched)
        data = json.loads(fetched.read_text())
        selected = {
            "file": local.name,
            "sha256": data["sha256"],
            "provider": data["provider"],
            "source": data["source_url"],
            "license": data["license_url"],
            "credit": data["credit"],
            "path": str(local),
            "pool": pool(content.get("kind")),
        }
    except (NetworkError, OSError):
        selected = choose(content)
    value = {"content_hash": content["content_hash"], "asset": selected}
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    with manifest.open("xb") as stream:
        stream.write(raw)
    manifest.chmod(0o440)
    with (directory / "footage.sha256").open("x") as stream:
        stream.write(hashlib.sha256(raw).hexdigest())
    return manifest


def pinned(content: dict[str, Any], manifest: Path) -> dict[str, Any]:
    """Offline contract: any changed prepared input is fatal, never a new backdrop."""
    if any(p.is_symlink() for p in (manifest, *manifest.absolute().parents)):
        raise ValueError("Unsafe prepared footage")
    raw = manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest() != (manifest.parent / "footage.sha256").read_text():
        raise ValueError("Prepared footage manifest changed")
    value = json.loads(raw)
    if value["content_hash"] != content["content_hash"]:
        raise ValueError("Prepared footage content mismatch")
    asset = value["asset"]
    path = Path(asset["path"])
    if asset["provider"] == "Mixkit":
        allowed = {str(ROOT / c["file"]): c["sha256"] for c in load()}
        if allowed.get(str(path)) != asset["sha256"]:
            raise ValueError("Unknown local footage")
    elif asset["provider"] not in {"pexels", "pixabay", "coverr"} or path.parent != manifest.parent:
        raise ValueError("Unknown prepared footage")
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
        raise ValueError("Prepared footage fingerprint changed")
    return dict(asset)
