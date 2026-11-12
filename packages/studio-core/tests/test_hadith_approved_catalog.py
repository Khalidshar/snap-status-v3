"""Approved-catalog boundary tests use only synthetic nonreligious text."""

import copy
import hashlib
import json

import pytest

from studio_core.religious import hadith_catalog, hadith_text_check

TEXT = "نَصٌّ اخْتِبَارِيٌّ غَيْرُ دِينِيٍّ"


def install(tmp_path, monkeypatch):
    root = tmp_path / "sources"
    root.mkdir()
    (root / "fixture.txt").write_bytes(TEXT.encode())
    row = {
        "id": "fixture",
        "text": TEXT,
        "sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
        "source_sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
        "unmarked_positions": [6, 13, 29],
        "approved": True,
        "source": "مصدر اختباري",
        "narrator": "اختبار",
        "grade": "صحيح",
        "narrator_url": "https://sunnah.com/bukhari:1",
    }
    data = {"schema": 1, "approved": True, "calendar": "umm_al_qura", "entries": [row]}
    path = tmp_path / "catalog.json"
    monkeypatch.setattr(hadith_text_check, "SOURCES", root)
    return path, data


def test_approved_catalog_requires_exact_snapshot_and_preserves_input(tmp_path, monkeypatch):
    path, data = install(tmp_path, monkeypatch)
    original = copy.deepcopy(data)
    path.write_text(json.dumps(data))
    assert hadith_catalog.load(path) == data["entries"]
    assert data == original
    (tmp_path / "sources/fixture.txt").write_bytes(TEXT.encode() + b"\n")
    with pytest.raises(ValueError, match="SOURCE_TEXT_CHANGED"):
        hadith_catalog.load(path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("approved", False),
        ("grade", "ضعيف"),
        ("id", "../outside"),
        ("sha256", []),
        ("source_sha256", "0"),
        ("source", ""),
        ("narrator", ""),
        ("narrator_url", None),
        ("narrator_url", "https://untrusted.example/a"),
        ("narrator_url", "https://sunnah.com/bukhari:1?x=1"),
    ],
)
def test_invalid_entry_is_never_returned(tmp_path, monkeypatch, field, value):
    path, data = install(tmp_path, monkeypatch)
    data["entries"][0][field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        hadith_catalog.load(path)


@pytest.mark.parametrize(
    "field,value",
    [("schema", True), ("entries", []), ("entries", [None]), ("calendar", "estimated")],
)
def test_schema_is_exact(tmp_path, monkeypatch, field, value):
    path, data = install(tmp_path, monkeypatch)
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        hadith_catalog.load(path)


def test_duplicates_and_unapproved_catalog_are_blocked(tmp_path, monkeypatch):
    path, data = install(tmp_path, monkeypatch)
    data["entries"] *= 2
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="DUPLICATE"):
        hadith_catalog.load(path)
    data["approved"] = False
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="NOT_APPROVED"):
        hadith_catalog.load(path)


def test_missing_oversized_and_symlink_catalog_are_blocked(tmp_path, monkeypatch):
    path, _ = install(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        hadith_catalog.load(path)
    path.write_bytes(b"x" * 2_000_001)
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        hadith_catalog.load(path)
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="UNSAFE"):
        hadith_catalog.load(alias)
