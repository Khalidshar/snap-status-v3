"""Integrity tests. Synthetic fixtures are never source Quran or bundled output."""

import hashlib
import importlib.util
import json
import os
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from snap_bot import faith


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("kind", faith.KINDS)
def test_bundled_kind(kind: str) -> None:
    items = faith.load(kind)
    assert (
        len(items)
        == {
            "adhkar_am": 9,
            "adhkar_pm": 11,
            "hadith": 8,
            "dua": 5,
            "fast": 7,
            "verse": 46,
            "friday": 3,
            "kahf": 1,
        }[kind]
    )
    for item in items:
        assert faith.text(item)


@pytest.fixture
def synthetic_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Exercise all paths without inventing or accepting any religious text."""
    original = faith.DATA
    for filename in ("adhkar.json", "hadith.json", "duas.json", "fast.json"):
        (tmp_path / filename).write_bytes((original / filename).read_bytes())
    adhkar = json.loads((tmp_path / "adhkar.json").read_text())
    refs = {(i["surah"], a) for i in adhkar["items"] if i["type"] == "quran" for a in i["ayahs"]}
    refs.update((18, a) for a in range(1, 11))
    refs.update((1, a) for a in range(1, 91 - len(refs)))
    assert len(refs) == 90
    items = [
        {
            "surah": s,
            "ayah": a,
            "text": "synthetic test fixture",
            "sha256": digest("synthetic test fixture"),
        }
        for s, a in sorted(refs)
    ]
    collections = {
        "verse": [{"id": f"verse-{i:02}", "surah": 18, "ayahs": [1]} for i in range(46)],
        "friday": [{"id": f"friday-{i:02}", "surah": 18, "ayahs": [2]} for i in range(3)],
        "kahf": [{"id": "kahf-01", "surah": 18, "ayahs": list(range(1, 11))}],
    }
    subset = {
        "schema": 1,
        "language": "ar",
        "sacred": True,
        "kind": "quran_subset",
        "source_repo": faith.SOURCE_REPO,
        "zip_url": faith.OFFICIAL_URL,
        "zip_sha256": faith.OFFICIAL_SHA256,
        "publication_date": "2026-09-30",
        "previous_zip_sha256": faith.PREVIOUS_SHA256,
        "checksum_note": "synthetic metadata fixture",
        "package_version": "kfgqpc_hafs_v30",
        "package_ayahs": 6236,
        "package_surahs": 114,
        "build_date": "2026-10-28",
        "collections": collections,
        "items": items,
    }
    (tmp_path / "quran_subset.json").write_text(json.dumps(subset))
    monkeypatch.setattr(faith, "DATA", tmp_path)
    return tmp_path


@pytest.mark.parametrize("kind", faith.KINDS)
def test_all_loader_paths_with_synthetic_subset(synthetic_data: Path, kind: str) -> None:
    assert faith.load(kind)


def test_subset_hashes_are_verified_once_for_a_complete_verse_load(synthetic_data: Path) -> None:
    faith._validated_subset.cache_clear()
    with patch.object(faith, "_verified", wraps=faith._verified) as verified:
        items = faith.load("verse")
        for item in items:
            assert faith.text(item) == "synthetic test fixture"
        assert verified.call_count == 90
        assert faith.load("verse") == items
        assert verified.call_count == 90


@pytest.mark.parametrize("change", ("same_size", "new_size", "restored_mtime"))
def test_cached_subset_rejects_a_changed_letter(synthetic_data: Path, change: str) -> None:
    path = synthetic_data / "quran_subset.json"
    faith._validated_subset.cache_clear()
    item = faith.load("verse")[0]
    assert faith.text(item) == "synthetic test fixture"  # prime and reuse the cache
    before = path.stat()
    raw = path.read_text()
    replacement = "synthetic test fixturx" if change != "new_size" else "synthetic test fixturex"
    changed = raw.replace('"synthetic test fixture"', json.dumps(replacement), 1)
    assert changed != raw
    path.write_text(changed)
    if change != "new_size":
        assert path.stat().st_size == before.st_size
    if change == "restored_mtime":
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    else:
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    with pytest.raises(ValueError, match="hash mismatch"):
        faith.text(item)
    with pytest.raises(ValueError, match="hash mismatch"):
        faith.load("verse")


def test_loaded_collections_cannot_change_cached_references(synthetic_data: Path) -> None:
    item = faith.load("verse")[0]
    item["ayahs"].append(999)
    item["id"] = "changed"
    loaded = faith.load("verse")[0]
    assert loaded["id"] != "changed" and loaded["ayahs"] == [1]
    assert faith.text(loaded) == "synthetic test fixture"


def test_subset_changed_during_validation_is_rejected(synthetic_data: Path) -> None:
    faith._validated_subset.cache_clear()
    original = faith._document
    path = synthetic_data / "quran_subset.json"

    def change_after_read(filename, kind):
        value = original(filename, kind)
        path.write_text(path.read_text() + " ")
        return value

    with patch.object(faith, "_document", side_effect=change_after_read):
        with pytest.raises(ValueError, match="changed during verification"):
            faith.load("verse")


@pytest.mark.parametrize(
    "filename", ("adhkar.json", "hadith.json", "duas.json", "fast.json", "quran_subset.json")
)
def test_any_single_character_change_fails(synthetic_data: Path, filename: str) -> None:
    path = synthetic_data / filename
    original = json.loads(path.read_text())
    for n, item in enumerate(original["items"]):
        if "text" not in item:
            continue
        bad = deepcopy(original)
        bad["items"][n]["text"] += "x"
        path.write_text(json.dumps(bad))
        kind = {
            "adhkar.json": "adhkar_am",
            "hadith.json": "hadith",
            "duas.json": "dua",
            "fast.json": "fast",
            "quran_subset.json": "verse",
        }[filename]
        with pytest.raises(ValueError, match="hash mismatch"):
            faith.load(kind)
    path.write_text(json.dumps(original))


@pytest.mark.parametrize("problem", ("missing_ayah", "header_sha", "sacred", "duplicate", "bidi"))
def test_bad_subset(synthetic_data: Path, problem: str) -> None:
    path = synthetic_data / "quran_subset.json"
    obj = json.loads(path.read_text())
    if problem == "missing_ayah":
        obj["items"][0]["surah"] = 114
        obj["items"][0]["ayah"] = 500  # preserve count while removing a referenced ayah
        obj["collections"]["verse"][0].update(surah=1, ayahs=[1])
    elif problem == "header_sha":
        obj["zip_sha256"] = "0" * 64
    elif problem == "sacred":
        obj["sacred"] = False
    elif problem == "duplicate":
        obj["items"][1] = obj["items"][0]
    elif problem == "bidi":
        obj["items"][0]["text"] += "\u202e"
        obj["items"][0]["sha256"] = digest(obj["items"][0]["text"])
    path.write_text(json.dumps(obj))
    with pytest.raises(ValueError):
        faith.load("verse")


@pytest.mark.parametrize("kind,filename", (("hadith", "hadith.json"), ("dua", "duas.json")))
@pytest.mark.parametrize(
    "problem", ("weak", "missing_grade", "sacred", "duplicate_id", "bidi", "control")
)
def test_bad_narration(synthetic_data: Path, kind: str, filename: str, problem: str) -> None:
    path = synthetic_data / filename
    obj = json.loads(path.read_text())
    if problem == "weak":
        obj["items"][0]["grade"] = "ضعيف"
    elif problem == "missing_grade":
        del obj["items"][0]["grade"]
    elif problem == "sacred":
        obj["sacred"] = False
    elif problem == "duplicate_id":
        obj["items"][1]["id"] = obj["items"][0]["id"]
    else:
        obj["items"][0]["text"] += "\u2067" if problem == "bidi" else "\n"
        obj["items"][0]["sha256"] = digest(obj["items"][0]["text"])
    path.write_text(json.dumps(obj))
    with pytest.raises(ValueError):
        faith.load(kind)


def test_exact_join_and_display(synthetic_data: Path) -> None:
    item = faith.load("kahf")[0]
    assert faith.text(item) == " ".join(["synthetic test fixture"] * 10)
    for kind in ("hadith", "dua"):
        for narration in faith.load(kind):
            assert faith.text(narration) == narration["text"]


def test_missing_subset_fails_closed(synthetic_data: Path) -> None:
    (synthetic_data / "quran_subset.json").unlink()
    for kind in ("adhkar_am", "adhkar_pm", "verse", "friday", "kahf"):
        with pytest.raises(ValueError, match="Missing or unreadable"):
            faith.load(kind)


def test_unknown_kind() -> None:
    with pytest.raises(ValueError):
        faith.load("../hadith")


def test_bad_archive_never_writes(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[3] / "tools/build_faith_data.py"
    spec = importlib.util.spec_from_file_location("build_faith_data_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "quran_subset.json"
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        module.build(b"unapproved archive", output)
    assert not output.exists()


# Canonical item hashes read from the pinned old repository, including all metadata.
SOURCE_ITEM_HASHES = {
    "adhkar": [
        "f273860f1aaaeb361bbdea5fe43340ccb101d5085e84bd53719787b291c226f8",
        "dae01306a54023c6b692a424292289b888308af9c51b6bdb31c2ec983d8f8b5b",
        "48fb0a13a598db614dac6dfd1ab7c380a21cad52bd878aa33753990fd782d735",
        "cab5339feaf9520946c50f2c02a13f573663a43697642072c96545e696f64697",
        "5bbfaa4e2882440c81a0df85a8471caa4d20d4d50b18eb4d3356ac7b2a508964",
        "f696aeb9fc34555ac475ba1c7187e742c75a78414bf474b80b5bd225e626a998",
        "1f36e77f2c544f88c2f2337af9f1c53d84722909bf9420937f77a4beb961400a",
        "a80cbb5809fb8928ae3066c9e2c2a68a8b924687788e52c6e2c4256250ae7294",
        "d5ac982e1d1a2ae402689e04698c8c24fd403663aca019a7f4bd4e250d64b55e",
        "30c2402226c632e12f156e5b5230892159428c4bab08c607d022dd268bfa2201",
        "77886c379f28f6174d2e9a32881a5eb4ed13e06c186c6cacd3e517acb9281b23",
        "fff72fb9ef4fdc36240b3bacf522492083a89a608bf6bd664857b68d366463fc",
        "222a475f2d49235aac51368738ded368c1502a1efc785545c731c18df1f4f386",
        "f2814f1a6e84ad3fa069f81263d5596a0a621221d57c4e0c92399b8e3bf11ee5",
        "111aceabbdf126fc56488a1ffc066c272fc2373f8c1da641863e9fe5ac892427",
        "34770b22cb27d5e1ddbde2ce212dc9552885b8cc9ccfba8b4ba55f58bddb2ac4",
        "10033e946b89c799144379133d1ef20126a851480a364e7e88e4f74b26790f19",
        "0fa1c5c575a23d157fc3e18c6749fa5cc1efd10169fab62cdaabac062fe3fbea",
        "6995f546970898e83f0a46fbea2ef37d167140dc12f1b68de0d136fc7c401a1d",
        "b05a53e0563ba0c6c259b5c1e474feea6c79b62512546c36bb913a49d082836f",
    ],
    "hadith": [
        "25257f90be9af9d78adb1fc768dcb052f6b0cc2ac8bf05d09b8bb50396989021",
        "b76d46bec407a19b380a7dccb139e153298fce0ee16486a885ed8759f9605284",
        "c78479fad54ea53cf30668baf89613f19d942ad9de61e06a2705fce34594eca8",
        "268d777f9869070f7697ca6db494262c2291d7119a51e1f1dfc76468f6299b4a",
        "afb28ac1cda77ac4897c5d453a418327933e961b71e3c704422256837a84dbfd",
        # Owner-approved 2026-10-29: only the ID and source number 2319 -> 2318 changed;
        # the source copy hashed 89b636a8... before that correction.
        "6dcbf2d4e4b205d27d90764a044a3a99099c268233b0acc708dc1f9b90f64ae6",
        "5f1e2c0fa523b6450a822d798b1d2df48d264f2a3c8e38f84f6a440d031b5c83",
        "94fe34c32af8a85de6f8eed6adfa3d43001caf3c77235eace822f70717e2a13a",
    ],
    "dua": [
        "6a45394e594a9f5484cc303d88da1b7e71bbf5f616a7d3837134d07d31a86dfa",
        "2838d9e72d43b91b72c3378df0152bb6b5952fec016eb7523f22969217770596",
        "973cd222c08aeed6b05727edfcdb2b0b197b34092bf1de75a41c267236790a6d",
        "28a9e9ff38b2ac03463df401953190cfc0bb5a75990d0f585c8ad5e699b4da36",
        "7a5e75dacfe50b26a497b925485481f4b35c59e00b5ed86c7991e975da842c18",
    ],
}


@pytest.mark.parametrize(
    "kind,filename", (("adhkar", "adhkar.json"), ("hadith", "hadith.json"), ("dua", "duas.json"))
)
def test_exact_source_copy(kind: str, filename: str) -> None:
    items = json.loads((faith.DATA / filename).read_text())["items"]
    hashes = []
    for item in items:
        original = {
            k: v
            for k, v in item.items()
            if k not in {"sha256", "period", "narrator", "narrator_url"}
        }
        if kind != "hadith":
            original.pop("id")
        hashes.append(digest(json.dumps(original, ensure_ascii=False, sort_keys=True)))
    assert hashes == SOURCE_ITEM_HASHES[kind]


def test_official_ayah_markers_preserved() -> None:
    subset = json.loads((faith.DATA / "quran_subset.json").read_text())
    for item in subset["items"]:
        assert "\u06dd" in item["text"]
        assert faith._verified(item, quran=True) == item["text"]
        with pytest.raises(ValueError, match="Unsafe"):
            faith._verified(item)  # not allowed in narration text


@pytest.mark.parametrize("control", ("\u202e", "\u2067", "\u200f", "\x00", "\n"))
def test_quran_still_rejects_controls(control: str) -> None:
    item = json.loads((faith.DATA / "quran_subset.json").read_text())["items"][0]
    item["text"] = control + item["text"]
    item["sha256"] = digest(item["text"])
    with pytest.raises(ValueError, match="Unsafe"):
        faith._verified(item, quran=True)


@pytest.mark.parametrize(
    "value", ("synthetic \u06dd", "\u06dd١ synthetic", "synthetic \u06dd١\u06dd٢")
)
def test_invalid_marker_position_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="Unsafe"):
        faith._verified({"text": value, "sha256": digest(value)}, quran=True)


@pytest.mark.parametrize(
    "field,value", (("zip_sha256", faith.PREVIOUS_SHA256), ("publication_date", "2026-09-29"))
)
def test_superseded_package_rejected(synthetic_data: Path, field: str, value: str) -> None:
    path = synthetic_data / "quran_subset.json"
    obj = json.loads(path.read_text())
    obj[field] = value
    path.write_text(json.dumps(obj))
    with pytest.raises(ValueError, match="Unapproved official"):
        faith.load("verse")


def test_builder_requires_unicode_source_field() -> None:
    script = Path(__file__).resolve().parents[3] / "tools/build_faith_data.py"
    spec = importlib.util.spec_from_file_location("build_faith_unicode_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base = {"sura_no": 1, "aya_no": 1, "aya_text": "alternate fixture"}
    assert module.rows_from_json(json.dumps([base]).encode()) is None
    base["aya_text_unicode"] = "  unchanged unicode fixture  "
    assert (
        module.rows_from_json(json.dumps([base]).encode())[0]["aya_text_unicode"]
        == base["aya_text_unicode"]
    )


# Locked after comparison with the linked source excerpts; never recompute expectations.
FAST_HASHES = {
    "fast-01": "9d6389ff7c43cd932c277eb893c548d82519e19bce42b9b16758b2adbf641223",
    "fast-02": "f4161d3f11bf56789147313cac34fd7747db8662535b94923c0c5153d2980a5f",
    "fast-03": "3c17c038368b136d99ad6b734089c924de9b6c26a1ee58940c864febaf1ce9ea",
    "fast-04": "cbab22422c586c93d3409346fd706d887438724765b388b4833b00e3359db0ec",
    "fast-05": "3f2969d9947989410a81e332c26e124a1fc7e9882b74a9dc3a1f7a4466d4bc9d",
    "fast-06": "2cedec823addf03d3aab2d826bed3943ced68f9b5a25adadab4f7c0b942a86a5",
    "fast-07": "d6d9235ea6336c6d38f96c8ae8ab729a7f03653147f4155a8e16898a99e11ed0",
}


def test_fast_pinned_hashes() -> None:
    items = faith.load("fast")
    assert {item["id"]: digest(item["text"]) for item in items} == FAST_HASHES
    assert all(item["sha256"] == FAST_HASHES[item["id"]] for item in items)


@pytest.mark.parametrize(
    "field,value",
    [
        ("grade", "حسن"),
        ("verification_url", "https://example.com/1"),
        ("verification_url", ""),
        ("topic", ""),
    ],
)
def test_fast_metadata_fails_closed(synthetic_data: Path, field: str, value: str) -> None:
    path = synthetic_data / "fast.json"
    data = json.loads(path.read_text())
    data["items"][0][field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        faith.load("fast")


@pytest.mark.parametrize("index", (5, 6))
@pytest.mark.parametrize(
    "field,value",
    (
        ("grade_by", None),
        ("grade_by", ""),
        ("grade_by", "مجهول"),
        ("grade_source", None),
        ("grade_source", "   "),
        ("grade_url", "https://example.com/grade"),
        ("grade_url", ""),
        ("grade", "حسن"),
    ),
)
def test_sunan_fast_requires_verified_sahih_grading(
    synthetic_data: Path, index: int, field: str, value: object
) -> None:
    path = synthetic_data / "fast.json"
    data = json.loads(path.read_text())
    data["items"][index][field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        faith.load("fast")


def test_sunan_fast_reviewed_references() -> None:
    added = faith.load("fast")[5:]
    assert [(i["topic"], i["verification_url"]) for i in added] == [
        ("الاثنين والخميس", "https://sunnah.com/tirmidhi:745"),
        ("الأيام البيض", "https://sunnah.com/abudawud:2449"),
    ]
    assert all(i["grade"] == "صحيح" and i["grade_by"] == "الألباني" for i in added)
    assert all(i["grade_source"] and i["grade_url"] for i in added)


@pytest.mark.parametrize("field", ("narrator", "narrator_url"))
@pytest.mark.parametrize("value", (None, "", 7, "\u202e", "\x00", "\n", "   "))
def test_hadith_narrator_metadata_required_and_safe(
    synthetic_data: Path, field: str, value: object
) -> None:
    path = synthetic_data / "hadith.json"
    obj = json.loads(path.read_text())
    obj["items"][0][field] = value
    path.write_text(json.dumps(obj))
    with pytest.raises(ValueError):
        faith.load("hadith")


@pytest.mark.parametrize(
    "url",
    (
        "http://sunnah.com/bukhari:1",
        "https://sunnah.com.evil.test/bukhari:1",
        "https://evil.test/bukhari:1",
        "https://sunnah.com/",
        "https://sunnah.com/bukhari:1?next=evil",
        "https://sunnah.com/bukhari:1#fragment",
    ),
)
def test_hadith_narrator_url_must_be_direct_trusted_source(synthetic_data: Path, url: str) -> None:
    path = synthetic_data / "hadith.json"
    obj = json.loads(path.read_text())
    obj["items"][0]["narrator_url"] = url
    path.write_text(json.dumps(obj))
    with pytest.raises(ValueError, match="Unapproved hadith narrator URL"):
        faith.load("hadith")


def test_hadith_narrators_match_reviewed_sources() -> None:
    expected = {
        "bukhari_1": ("عمر بن الخطاب", "bukhari:1"),
        "muslim_2564": ("أبو هريرة", "muslim:2564a"),
        "bukhari_6018": ("أبو هريرة", "bukhari:6018"),
        "muslim_2699": ("أبو هريرة", "muslim:2699a"),
        "bukhari_13": ("أنس بن مالك", "bukhari:13"),
        "muslim_2318": ("أبو هريرة", "muslim:2318a"),
        "bukhari_6114": ("أبو هريرة", "bukhari:6114"),
        "muslim_41": ("جابر بن عبد الله", "muslim:41"),
    }
    for item in faith.load("hadith"):
        name, reference = expected[item["id"]]
        assert item["narrator"] == name + " رضي الله عنه"
        assert item["narrator_url"] == "https://sunnah.com/" + reference
        assert digest(item["text"]) == item["sha256"]


# Pinned before adding narrator/speaker; every earlier field stays byte-identical.
FAST_LEGACY_ITEM_HASHES = [
    "72f29d9c8aa36323c915ed991142484ec0aed3dd434d66ce5f66bb5f3e0f1d59",
    "48718da7c8264a0464ecfa3137355901f31b02e8b072cc6bdd62b2b27f43a5e8",
    "725f26bef4cc78970896e09765ad2436a341034f1e5db810b67086a7a6727f17",
    "8c16ebc9862b055a78b154a999c6ce7564f506c6d56e97419cc93bc2a465c2b4",
    "f87141f4fa75bb85f91e777f39824d8d8cd41a7e2ea90c4ceb1b0fe021e78d8c",
    "7c8314262609a3de194e3e54b5d18e18751dce1642a78be67e7d8f0bc8c95d82",
    "758ad6e39f46e142d2992d7879ae9cd170843031ab2d92893ae51566f48753e5",
]


def test_fast_narrator_additions_preserve_original_records() -> None:
    hashes = []
    for item in faith.load("fast"):
        original = {k: v for k, v in item.items() if k not in {"narrator", "speaker"}}
        hashes.append(digest(json.dumps(original, ensure_ascii=False, sort_keys=True)))
    assert hashes == FAST_LEGACY_ITEM_HASHES


@pytest.mark.parametrize("index", range(7))
@pytest.mark.parametrize(
    "field,value",
    (
        ("narrator", None),
        ("narrator", ""),
        ("narrator", "   "),
        ("narrator", 42),
        ("narrator", "راوٍ\u202e"),
        ("narrator", "راوٍ\n"),
        ("speaker", None),
        ("speaker", ""),
        ("speaker", "Prophet"),
        ("speaker", "other"),
        ("speaker", []),
    ),
)
def test_fast_narrator_and_speaker_fail_closed(
    synthetic_data: Path, index: int, field: str, value: object
) -> None:
    path = synthetic_data / "fast.json"
    obj = json.loads(path.read_text())
    obj["items"][index][field] = value
    path.write_text(json.dumps(obj))
    with pytest.raises(ValueError):
        faith.load("fast")


@pytest.mark.parametrize("field", ("narrator", "speaker"))
def test_fast_narrator_and_speaker_cannot_be_missing(synthetic_data: Path, field: str) -> None:
    path = synthetic_data / "fast.json"
    obj = json.loads(path.read_text())
    del obj["items"][0][field]
    path.write_text(json.dumps(obj))
    with pytest.raises(ValueError):
        faith.load("fast")


def test_fast_narrators_and_speakers_match_linked_sources() -> None:
    expected = [
        ("أبو قتادة الأنصاري رضي الله عنه", "prophet", "https://sunnah.com/muslim:1162e"),
        ("أبو هريرة رضي الله عنه", "companion", "https://sunnah.com/bukhari:1981"),
        ("أبو قتادة رضي الله عنه", "prophet", "https://sunnah.com/muslim:1162a"),
        ("أبو قتادة رضي الله عنه", "prophet", "https://sunnah.com/muslim:1162a"),
        ("أبو أيوب الأنصاري رضي الله عنه", "prophet", "https://sunnah.com/muslim:1164a"),
        ("عائشة رضي الله عنها", "companion", "https://sunnah.com/tirmidhi:745"),
        ("قتادة بن ملحان القيسي رضي الله عنه", "companion", "https://sunnah.com/abudawud:2449"),
    ]
    assert [
        (i["narrator"], i["speaker"], i["verification_url"]) for i in faith.load("fast")
    ] == expected
