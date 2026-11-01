import hashlib
import json
from unittest.mock import patch

import pytest

from snap_bot import footage
from studio_core.network.client import NetworkError


def test_local_fallback_is_pinned_and_mutation_is_fatal(tmp_path):
    content = {"body": "نص", "kind": "hadith", "content_hash": "a" * 64}
    with patch("snap_bot.video_backgrounds.prefetch", side_effect=NetworkError("offline")):
        manifest = footage.prepare(content, tmp_path / "background", tmp_path / "unused.sock")
    selected = footage.pinned(content, manifest)
    assert selected["provider"] == "Mixkit" and selected["pool"] == "calm"
    assert footage.prepare(content, manifest.parent, tmp_path / "unused") == manifest
    manifest.chmod(0o600)
    manifest.write_text("{}")
    with pytest.raises(ValueError, match="manifest changed"):
        footage.pinned(content, manifest)


def test_live_asset_stays_local_and_content_bound(tmp_path):
    content = {"body": "نص", "content_hash": "b" * 64}
    data = b"\0\0\0\x18ftypisom"

    def fetched(query, directory, socket):
        path = directory / "video.mp4"
        path.write_bytes(data)
        manifest = directory / "background.json"
        manifest.write_text(
            json.dumps(
                {
                    "file": path.name,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "provider": "pexels",
                    "source_url": "https://pexels.com/video/1",
                    "license_url": "https://pexels.com/license/",
                    "credit": "author",
                }
            )
        )
        return manifest

    with patch("snap_bot.video_backgrounds.prefetch", side_effect=fetched):
        manifest = footage.prepare(content, tmp_path / "background", tmp_path / "unused.sock")
    assert footage.pinned(content, manifest)["provider"] == "pexels"
    with pytest.raises(ValueError, match="content mismatch"):
        footage.pinned({**content, "content_hash": "c" * 64}, manifest)
    (manifest.parent / "video.mp4").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="fingerprint changed"):
        footage.pinned(content, manifest)
