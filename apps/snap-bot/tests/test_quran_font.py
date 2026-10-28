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
def charsets() -> dict[str, str]:
    executable = shutil.which("fc-query")
    if executable is None:
        pytest.skip("fc-query is absent locally; CI must provide fontconfig")
    result = {}
    # Query only the exact selected font, never a fontconfig fallback.
    for kind in ("quran", "hadith"):
        try:
            selected = quran_font.font_for(kind)
        except ValueError as error:
            # Keep Quran coverage runnable even if the separate Noto Naskh asset is missing.
            result[kind] = "ERROR: " + str(error)
            continue
        query = subprocess.run(
            [executable, "--format=%{charset}", str(selected)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert query.stdout.strip(), f"Empty charset: {selected}"
        result[kind] = query.stdout
    return result


def assert_covered(charset: str, texts: list[tuple[str, str]], section: str) -> None:
    assert not charset.startswith("ERROR: "), f"{section}: {charset}"
    ranges = []
    for item in charset.split():
        ends = item.split("-")
        ranges.append((int(ends[0], 16), int(ends[-1], 16)))
    missing: dict[int, list[str]] = {}
    for identifier, text in texts:
        for char in text:
            if not any(low <= ord(char) <= high for low, high in ranges):
                missing.setdefault(ord(char), []).append(identifier)
    details = [
        f"U+{code:04X} {chr(code)} {unicodedata.name(chr(code), 'UNKNOWN')}: "
        + ", ".join(sorted(set(ids)))
        for code, ids in sorted(missing.items())
    ]
    assert not missing, f"{section}: missing font characters\n" + "\n".join(details)


def test_quran_character_coverage(charsets: dict[str, str]) -> None:
    # Validate the subset via faith first; inspect ALL bundled ayahs, not only
    # verses selected by one menu entry. Exact stored text, no normalization.
    faith.load("verse")
    subset = json.loads((faith.DATA / "quran_subset.json").read_text())
    texts = [(f"{i['surah']}:{i['ayah']}", i["text"]) for i in subset["items"]]
    for kind in ("adhkar_am", "adhkar_pm"):
        texts.extend((i["id"], faith.text(i)) for i in faith.load(kind) if i["type"] == "quran")
    assert_covered(charsets["quran"], texts, "Quran / KFGQPC Hafs")


def test_non_quran_character_coverage(charsets: dict[str, str]) -> None:
    texts = []
    for kind in faith.KINDS:
        if kind in ("verse", "friday", "kahf"):
            continue
        texts.extend((i["id"], faith.text(i)) for i in faith.load(kind) if i.get("type") != "quran")
    assert_covered(charsets["hadith"], texts, "Narrations / Noto Naskh Arabic Regular")


@pytest.mark.parametrize("kind", ("quran", "verse", "friday", "kahf"))
def test_quran_kind_routes_to_hafs(kind: str) -> None:
    assert quran_font.font_for(kind) == quran_font.path()


@pytest.mark.parametrize(
    "item",
    (
        {"type": "quran", "surah": 2, "ayahs": [255]},
        {"surah": 18, "ayahs": [1, 2]},
        {"surah": 18, "ayah": 1, "text": "synthetic routing fixture"},
    ),
)
def test_quran_item_routes_to_hafs(item: dict) -> None:
    assert quran_font.font_for(item) == quran_font.path()


@pytest.mark.parametrize(
    "value",
    (
        "hadith",
        "dua",
        {"type": "hadith"},
        {"text": "synthetic routing fixture", "source": "fixture", "grade": "fixture"},
    ),
)
def test_non_quran_routes_only_to_existing_naskh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | dict
) -> None:
    # Synthetic bytes exercise routing only; never pretend to test glyph coverage.
    asset = tmp_path / "NotoNaskhArabic-Regular.ttf"
    asset.write_bytes(b"routing fixture")
    monkeypatch.setattr(quran_font, "NASKH", asset)
    monkeypatch.setattr(quran_font, "NASKH_SHA256", hashlib.sha256(asset.read_bytes()).hexdigest())
    assert quran_font.font_for(value) == asset
    asset.unlink()
    with pytest.raises(ValueError, match="Missing Noto Naskh"):
        quran_font.font_for(value)


def test_changed_naskh_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asset = tmp_path / "NotoNaskhArabic-Regular.ttf"
    asset.write_bytes(b"modified font fixture")
    monkeypatch.setattr(quran_font, "NASKH", asset)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        quran_font.font_for("hadith")


@pytest.mark.parametrize(
    "value",
    (
        "adhkar_am",
        "adhkar_pm",
        "unknown",
        {},
        {"type": "unknown"},
        {"type": "hadith", "surah": 2, "ayahs": [1]},
    ),
)
def test_unknown_or_mixed_kind_rejected(value: str | dict) -> None:
    with pytest.raises(ValueError):
        quran_font.font_for(value)
