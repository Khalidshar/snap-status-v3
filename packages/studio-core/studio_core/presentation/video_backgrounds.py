"""Prefetch before render; local assets and provenance are fingerprinted."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode, urlsplit, urlunsplit

from studio_core.config import Config
from studio_core.network.client import NetworkClient, NetworkError
from studio_core.network.video import VideoPolicy

PROVIDERS = ("pexels", "pixabay", "coverr")
LICENSES = {
    "pexels": "https://www.pexels.com/license/",
    "pixabay": "https://pixabay.com/service/license-summary/",
    "coverr": "https://coverr.co/license",
}


def candidates(provider: str, data: dict[str, Any]) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    for item in data.get("videos" if provider == "pexels" else "hits", [])[:10]:
        if provider == "pexels":
            files = [
                f
                for f in item.get("video_files", [])
                if f.get("file_type") == "video/mp4" and f.get("height", 0) >= 720
            ]
            files.sort(key=lambda f: f.get("height", 0))
            links = [f["link"] for f in files[:3]]
            source, credit = item.get("url", ""), item.get("user", {}).get("name", "Pexels")
        elif provider == "pixabay":
            links = [
                item.get("videos", {}).get(size, {}).get("url", "")
                for size in ("medium", "small", "large")
            ]
            source, credit = item.get("pageURL", ""), item.get("user", "Pixabay")
        else:
            links = [item.get("urls", {}).get("mp4", "")]
            source, credit = "https://coverr.co", "Coverr"
        result.extend((link, source, credit) for link in links if link)
    return result


def verify(manifest: Path) -> Path:
    value = json.loads(manifest.read_text())
    path: Path = manifest.parent / str(value["file"])
    if path.parent != manifest.parent or path.is_symlink():
        raise ValueError("ASSET_PATH_DENIED")
    if hashlib.sha256(path.read_bytes()).hexdigest() != value["sha256"]:
        raise ValueError("ASSET_CHANGED")
    return path


def prefetch(query: str, directory: Path, socket: Path) -> Path:
    if not query.strip() or len(query) > 200:
        raise ValueError("INVALID_QUERY")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = directory / "background.json"
    if manifest.exists():
        verify(manifest)
        return manifest
    client = NetworkClient(cast(Config, VideoPolicy(socket)))
    endpoints = {
        "pexels": "https://api.pexels.com/v1/videos/search?"
        + urlencode({"query": query, "orientation": "portrait", "per_page": 5}),
        "pixabay": "https://pixabay.com/api/videos/?"
        + urlencode({"q": query, "safesearch": "true", "per_page": 5}),
        "coverr": "https://api.coverr.co/videos?"
        + urlencode({"query": query, "urls": "true", "page_size": 5}),
    }
    for provider in PROVIDERS:
        try:
            choices = candidates(provider, json.loads(client.get(endpoints[provider]).body))
        except (NetworkError, ValueError, KeyError, TypeError, AttributeError):
            continue
        for url, source, credit in choices:
            try:
                response = client.get(url)
                body = response.body
                if response.content_type != "video/mp4" or len(body) < 12 or body[4:8] != b"ftyp":
                    continue
                digest = hashlib.sha256(body).hexdigest()
                path = directory / (digest + ".mp4")
                with path.open("xb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                path.chmod(0o440)
                parsed = urlsplit(url)
                public_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
                value = {
                    "provider": provider,
                    "file": path.name,
                    "sha256": digest,
                    "bytes": len(body),
                    "source_url": source.split("?")[0],
                    "download_url": public_url,
                    "license_url": LICENSES[provider],
                    "credit": credit,
                    "attribution_required": True,
                }
                with manifest.open("x") as handle:
                    json.dump(value, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                manifest.chmod(0o440)
                return manifest
            except (NetworkError, ValueError, KeyError, TypeError):
                continue
    raise NetworkError("ALL_VIDEO_PROVIDERS_FAILED")
