"""Font integrity and exact character coverage, with no render or network."""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import unicodedata
from pathlib import Path

import pytest

from snap_bot import faith, quran_font


def test_bundled_font() -> None:
    manifest = quran_font.verify()
    font = quran_font.path()
    assert font.name == manifest["file"]
    assert hashlib.sha256(font.read_bytes()).hexdigest() == manifest["sha256"]


@pytest.fixture
def font_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in (quran_font.FILENAME, "manifest.json"):
        shutil.copyfile(quran_font.DIRECTORY / name, tmp_path / name)
    monkeypatch.setattr(quran_font, "DIRECTORY", tmp_path)
    return tmp_path


def test_changed_byte_rejected(font_copy: Path) -> None:
    font = font_copy / quran_font.FILENAME
    data = bytearray(font.read_bytes())
    data[-1] ^= 1
    font.write_bytes(data)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        quran_font.verify()
    with pytest.raises(ValueError):
        quran_font.path()


@pytest.mark.parametrize(
    "field,value",
    (
        ("source_zip_sha256", "0" * 64),
        ("sha256", "0" * 64),
        ("file", "../other.ttf"),
        ("schema", True),
        ("license_note", ""),
    ),
)
def test_bad_manifest(font_copy: Path, field: str, value: object) -> None:
    manifest_path = font_copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        quran_font.verify()


def test_rehashed_changed_font_rejected(font_copy: Path) -> None:
    font = font_copy / quran_font.FILENAME
    font.write_bytes(font.read_bytes() + b"changed")
    manifest_path = font_copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sha256"] = hashlib.sha256(font.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        quran_font.verify()


@pytest.mark.parametrize("name", ("manifest.json", quran_font.FILENAME))
def test_missing_file_rejected(font_copy: Path, name: str) -> None:
    (font_copy / name).unlink()
    with pytest.raises(ValueError):
        quran_font.verify()


def test_bad_archive_never_extracts(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[3] / "tools/fetch_quran_font.py"
    spec = importlib.util.spec_from_file_location("fetch_quran_font_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match="archive SHA-256"):
        module.extract(b"unapproved zip", tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.fixture(scope="module")
def charset() -> str:
    executable = shutil.which("fc-query")
    if executable is None:
        pytest.skip("fc-query is absent locally; CI must provide fontconfig")
    # Same argv/charset query as snap_bot.render; never fontconfig fallback lookup.
    result = subprocess.run(
        [executable, "--format=%{charset}", str(quran_font.path())],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.stdout.strip(), "fc-query returned an empty charset"
    return result.stdout


@pytest.mark.parametrize("kind", faith.KINDS)
def test_every_faith_character_covered(charset: str, kind: str) -> None:
    ranges = []
    for item in charset.split():
        ends = item.split("-")
        ranges.append((int(ends[0], 16), int(ends[-1], 16)))
    missing: dict[int, list[str]] = {}
    # Check the exact text() output: do not strip, normalise or remove any marks.
    for item in faith.load(kind):
        for char in faith.text(item):
            if not any(low <= ord(char) <= high for low, high in ranges):
                missing.setdefault(ord(char), []).append(item["id"])
    details = [
        f"U+{code:04X} {chr(code)} {unicodedata.name(chr(code), 'UNKNOWN')}: "
        + ", ".join(sorted(set(ids)))
        for code, ids in sorted(missing.items())
    ]
    assert not missing, f"{kind}: missing font characters\n" + "\n".join(details)
