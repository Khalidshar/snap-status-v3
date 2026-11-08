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


FAITH = ROOT / "faith"
SHOT, FADE = 4.5, 0.8


def faith(root: Path = FAITH) -> list[dict[str, Any]]:
    """Reviewed Islamic backdrops (owner decision 8 Nov 2026, «تلاوة»), pinned by fingerprint."""
    clips: list[dict[str, Any]] = json.loads((root / "manifest.json").read_text(encoding="utf-8"))[
        "clips"
    ]
    for clip in clips:
        path = root / clip["file"]
        if (
            clip["provider"] != "Pexels"
            or clip["license"] != "Pexels License"
            or path.is_symlink()
            or path.parent != root
            or hashlib.sha256(path.read_bytes()).hexdigest() != clip["sha256"]
        ):
            raise ValueError("Footage fingerprint or license changed")
    return [{**c, "path": str(root / c["file"])} for c in clips]


def montage_order(seed: str) -> list[str]:
    """Islamic and calm nature backdrops taking turns (owner: «إسلامية مع خلفيات أخرى»)."""
    islamic = [c["path"] for c in faith()]
    nature = [str(ROOT / c["file"]) for c in load() if c["pool"] in {"calm", "night"}]
    start = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    islamic = islamic[start % len(islamic) :] + islamic[: start % len(islamic)]
    nature = nature[start % len(nature) :] + nature[: start % len(nature)]
    order: list[str] = []
    for i in range(max(len(islamic), len(nature))):
        order.append(islamic[i % len(islamic)])
        if i % 2 == 1:  # two Islamic shots for every nature shot
            order.append(nature[(i // 2) % len(nature)])
    return order


def montage_command(seed: str, duration: float, out: Path) -> list[str]:
    """Darkened warm backdrops changing every 4.5 s with a soft fade, as in the approved sample."""
    order = montage_order(seed)
    shots = int((duration - FADE) // (SHOT - FADE)) + 2
    args = ["/usr/bin/ffmpeg", "-v", "error", "-nostdin", "-y", "-threads", "2"]
    graph = []
    for k in range(shots):
        args += ["-stream_loop", "-1", "-i", order[k % len(order)]]
        graph.append(
            f"[{k}:v]trim=start={(k * 1.7) % 4:.1f}:duration={SHOT},setpts=PTS-STARTPTS,"
            "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,"
            "eq=brightness=-0.20:saturation=0.65:contrast=1.04,"
            "colorchannelmixer=rr=1.07:gg=0.97:bb=0.80,gblur=sigma=1.2,format=yuv420p"
            f"[b{k}]"
        )
    last = "b0"
    for k in range(1, shots):
        graph.append(
            f"[{last}][b{k}]xfade=transition=fade:duration={FADE}:offset={k * (SHOT - FADE):.3f}[x{k}]"
        )
        last = f"x{k}"
    graph.append(f"[{last}]trim=duration={duration:.3f}[v]")
    return [
        *args,
        "-filter_complex",
        ";".join(graph),
        "-map",
        "[v]",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        str(out),
    ]
