import json
import os

from studio_core.production.pipeline import expire_backgrounds


def test_expire_only_downloaded_background_preserves_evidence(tmp_path):
    folder = tmp_path / "job/attempts/1/input/background"
    folder.mkdir(parents=True)
    digest = "a" * 64
    raw = folder / (digest + ".mp4")
    raw.write_bytes(b"downloaded")
    keep = folder / "other.mp4"
    keep.write_bytes(b"unrelated")
    manifest = folder / "background.json"
    manifest.write_text(json.dumps({"file": raw.name, "sha256": digest, "provider": "pexels"}))
    os.utime(raw, (0, 0))
    expire_backgrounds(tmp_path, 7 * 86400)
    assert not raw.exists()
    assert manifest.is_file() and keep.is_file()
