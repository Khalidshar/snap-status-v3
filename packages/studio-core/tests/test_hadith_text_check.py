"""Only synthetic Arabic fixture text: no religious text is corrected or added."""

import hashlib
import importlib.util
import json
import unicodedata
from pathlib import Path

import pytest

from studio_core.religious import hadith_catalog
from studio_core.religious import hadith_text_check as gate

TEXT = "نَصٌّ اخْتِبَارِيٌّ غَيْرُ دِينِيٍّ"
EXCEPTIONS = [6, 13, 29]  # Reviewed only for this fixed nonreligious test string.
ROOT = Path(__file__).resolve().parents[3]


def proof(tmp_path, text=TEXT):
    (tmp_path / "fixture.txt").write_bytes(text.encode())
    return {
        "id": "fixture",
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "unmarked_positions": EXCEPTIONS.copy(),
    }


def test_exact_source_and_reviewed_long_vowels_pass_without_rewriting(tmp_path):
    item = proof(tmp_path)
    before = (tmp_path / "fixture.txt").read_bytes()
    assert gate.missing_positions(TEXT) == EXCEPTIONS
    gate.verify(item, root=tmp_path)
    assert item["text"] == TEXT
    assert (tmp_path / "fixture.txt").read_bytes() == before


@pytest.mark.parametrize("text", ["نص", "نّ", "نَّّ", "نَِ", "نَ\u0301", "َن", "a", "نَ\u200f"])
def test_missing_conflicting_or_unsupported_marks_fail(text):
    with pytest.raises(ValueError):
        gate.verify_diacritics(text, [])


def test_a_new_missing_vowel_cannot_be_approved_as_a_long_vowel_exception():
    # Removing the first vowel makes the first consonant invalid even if listed.
    changed = TEXT.replace("نَ", "ن", 1)
    with pytest.raises(ValueError, match="EXCEPTIONS_INVALID"):
        gate.verify_diacritics(changed, gate.missing_positions(changed))
    with pytest.raises(ValueError, match="EXCEPTIONS_INVALID"):
        gate.verify_diacritics("نّ", [0])


@pytest.mark.parametrize("exceptions", [None, [], [6, 13], [6, 13, 29, 0], [6, 13, 29, 29], [True]])
def test_exception_list_is_required_exact_and_reviewed(exceptions):
    with pytest.raises(ValueError):
        gate.verify_diacritics(TEXT, exceptions)


@pytest.mark.parametrize("suffix", [" ", "\n", "ِ"])
def test_resealed_text_cannot_override_source_snapshot(tmp_path, suffix):
    item = proof(tmp_path)
    item["text"] += suffix
    item["sha256"] = hashlib.sha256(item["text"].encode()).hexdigest()
    with pytest.raises(ValueError, match="SOURCE_TEXT_CHANGED"):
        gate.verify(item, root=tmp_path)


def test_source_hash_and_unicode_bytes_are_not_normalized(tmp_path):
    item = proof(tmp_path)
    (tmp_path / "fixture.txt").write_bytes(unicodedata.normalize("NFD", TEXT).encode() + b" ")
    with pytest.raises(ValueError, match="SOURCE_TEXT_CHANGED"):
        gate.verify(item, root=tmp_path)
    (tmp_path / "fixture.txt").write_bytes(TEXT.encode())
    item["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SOURCE_TEXT_CHANGED"):
        gate.verify(item, root=tmp_path)


def test_missing_oversized_or_symlink_snapshot_cannot_pass(tmp_path):
    item = proof(tmp_path)
    target = tmp_path / "fixture.txt"
    target.unlink()
    with pytest.raises(ValueError, match="SNAPSHOT_MISSING"):
        gate.verify(item, root=tmp_path)
    target.write_bytes(b"x" * 64001)
    with pytest.raises(ValueError, match="SOURCE_TEXT_CHANGED"):
        gate.verify(item, root=tmp_path)
    target.unlink()
    other = tmp_path / "other.txt"
    other.write_bytes(TEXT.encode())
    target.symlink_to(other)
    with pytest.raises(ValueError, match="SOURCE_UNSAFE"):
        gate.verify(item, root=tmp_path)


def test_ci_placeholder_is_explicit_and_never_emits_the_body(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "text_gate", ROOT / "scripts/check_hadith_catalog.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "pending.json"
    monkeypatch.setattr(hadith_catalog, "CATALOG", path)
    pending = {"schema": 1, "approved": False, "calendar": "umm_al_qura", "entries": []}
    path.write_text(json.dumps(pending))
    assert module.main() == 0
    assert "PENDING_OWNER_APPROVAL" in capsys.readouterr().out
    pending["entries"] = [{"text": TEXT}]
    path.write_text(json.dumps(pending))
    assert module.main() == 1
    output = capsys.readouterr().out
    assert "CHECK_FAILED" in output and TEXT not in output
