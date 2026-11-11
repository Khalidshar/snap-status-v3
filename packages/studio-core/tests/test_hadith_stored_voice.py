"""Missing/changed/unapproved recordings stop both bots without a synthesis fallback."""

import hashlib
import json

import pytest

from studio_core.production.prepare import Factory
from studio_core.religious import hadith_voice

TEXT = "نَصٌّ اخْتِبَارِيٌّ لَيْسَ حَدِيثًا"
RAW = b"ID3synthetic bytes for binding tests only"


def manifest(root, **changes):
    root.mkdir(exist_ok=True)
    row = {
        "id": "test",
        "approved": True,
        "text_sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
        "audio_sha256": hashlib.sha256(RAW).hexdigest(),
        **changes,
    }
    (root / "manifest.json").write_text(json.dumps({"schema": 1, "recordings": [row]}))
    (root / "test.mp3").write_bytes(RAW)


def test_audio_and_exact_text_are_bound_without_unicode_normalization(tmp_path):
    manifest(tmp_path)
    assert hadith_voice.read("test", TEXT, root=tmp_path) == RAW
    for changed in (TEXT + " ", TEXT.replace("نَ", "ن"), TEXT.replace("إ", "ا") + "x"):
        with pytest.raises(ValueError, match="TEXT_OR_HASH_CHANGED"):
            hadith_voice.read("test", changed, root=tmp_path)
    (tmp_path / "test.mp3").write_bytes(RAW + b"changed")
    with pytest.raises(ValueError, match="FILE_CHANGED"):
        hadith_voice.read("test", TEXT, root=tmp_path)


@pytest.mark.parametrize("change", [{"approved": False}, {"audio_sha256": "invalid"}])
def test_unapproved_or_invalid_manifest_is_rejected(tmp_path, change):
    manifest(tmp_path, **change)
    with pytest.raises(ValueError):
        hadith_voice.read("test", TEXT, root=tmp_path)


def test_missing_file_or_manifest_and_unsafe_identifiers_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        hadith_voice.read("test", TEXT, root=tmp_path)
    manifest(tmp_path)
    (tmp_path / "test.mp3").unlink()
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        hadith_voice.read("test", TEXT, root=tmp_path)
    for identifier in ("../test", "/test", "test.mp3"):
        with pytest.raises(ValueError, match="ID_INVALID"):
            hadith_voice.read(identifier, TEXT, root=tmp_path)


@pytest.mark.parametrize("app", ["snap-bot-v2", "status-bot-v2"])
def test_missing_recording_stops_factory_before_any_speech_request(app, tmp_path, monkeypatch):
    class NoNetwork:
        def get(self, url):
            raise AssertionError("Must never call a speech provider")

    monkeypatch.setattr(hadith_voice, "ROOT", tmp_path / "missing")
    f = Factory(
        app, "hadith", "e" * 32, tmp_path / "work", client=NoNetwork(), state=tmp_path / "state"
    )
    with pytest.raises(ValueError, match="HADITH_VOICE_UNAVAILABLE"):
        f.build()
    assert not (f.root / "content.json").exists()
