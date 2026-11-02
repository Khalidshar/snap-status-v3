"""Download only an explicitly licensed enclosure attached to this exact source item."""

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from studio_core.network.client import NetworkError


def prepare(
    content: dict[str, Any], directory: Path, client: Any, policy: dict[str, Any]
) -> Path | None:
    url = content.get("source_video_url")
    hosts = policy.get(str(content.get("source_id")), [])
    if not isinstance(url, str) or urlsplit(url).hostname not in hosts:
        return None
    if urlsplit(url).query or urlsplit(url).fragment or urlsplit(url).scheme != "https":
        return None
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    manifest = directory / "original.json"
    if manifest.exists():
        verify(content, manifest)
        return manifest
    try:
        response = client.get(url)
    except NetworkError:
        return None
    if (
        response.content_type != "video/mp4"
        or not 12 <= len(response.body) <= 50 * 1024 * 1024
        or response.body[4:8] != b"ftyp"
    ):
        return None
    video = directory / "original.mp4"
    with video.open("xb") as stream:
        stream.write(response.body)
    value = {
        "content_hash": content["content_hash"],
        "fingerprint": content["fingerprint"],
        "source_url": content["source_url"],
        "file": video.name,
        "sha256": hashlib.sha256(response.body).hexdigest(),
    }
    with manifest.open("x") as stream:
        json.dump(value, stream, sort_keys=True)
    video.chmod(0o440)
    manifest.chmod(0o440)
    return manifest


def verify(content: dict[str, Any], manifest: Path) -> Path:
    value = json.loads(manifest.read_text())
    for field in ("content_hash", "fingerprint", "source_url"):
        if value.get(field) != content.get(field):
            raise ValueError("SOURCE_EVENT_MISMATCH")
    path: Path = manifest.parent / str(value["file"])
    if path.parent != manifest.parent or any(
        p.is_symlink() for p in (path, manifest, *manifest.absolute().parents)
    ):
        raise ValueError("SOURCE_VIDEO_PATH_DENIED")
    if hashlib.sha256(path.read_bytes()).hexdigest() != value["sha256"]:
        raise ValueError("SOURCE_VIDEO_CHANGED")
    return path
