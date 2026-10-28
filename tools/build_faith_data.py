"""Build only from the pinned official package. Never import or run the old bot.

Run manually once; CI must never download. A local --zip permits offline checks
of the same bytes. A mismatch stops before reading any text or writing output.

Owner authorized the republished 2026-09-30 package on 2026-10-28.
The previous checksum belongs to an earlier package version.
"""

import argparse
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

OFFICIAL_URL = "https://download.qurancomplex.gov.sa/resources_dev/kfgqpc_hafs_v30.zip"
OFFICIAL_SHA256 = "227E6B1564D980F2BD09C2C35EBFB0330AC268C79A7C247CD1AB665BC635F245"
PREVIOUS_SHA256 = "6D4B6B2B6A290BDA1D8E3179BC6AAB7D49F6FAEDD3F2D31F7D589D5E6E99F8F4"
PUBLICATION_DATE = "2026-09-30"
OUTPUT = Path(__file__).resolve().parents[1] / "apps/snap-bot/snap_bot/data/faith/quran_subset.json"

# Reference structure copied from status-bot@b445281; contains no sacred text.
MANIFEST = {
    "collections": {
        "verse": [
            {"id": "verse-01", "surah": 2, "ayahs": [152], "scene": "ripple"},
            {"id": "verse-02", "surah": 2, "ayahs": [153], "scene": "dawn"},
            {"id": "verse-03", "surah": 2, "ayahs": [186], "scene": "ascend"},
            {"id": "verse-04", "surah": 3, "ayahs": [139], "scene": "dawn"},
            {"id": "verse-05", "surah": 13, "ayahs": [28], "scene": "ripple"},
            {"id": "verse-06", "surah": 14, "ayahs": [7], "scene": "growth"},
            {"id": "verse-07", "surah": 16, "ayahs": [128], "scene": "stars"},
            {"id": "verse-08", "surah": 21, "ayahs": [87], "scene": "ocean"},
            {"id": "verse-09", "surah": 29, "ayahs": [69], "scene": "dawn"},
            {"id": "verse-10", "surah": 33, "ayahs": [41, 42], "scene": "ripple"},
            {"id": "verse-11", "surah": 33, "ayahs": [56], "scene": "geometric"},
            {"id": "verse-12", "surah": 39, "ayahs": [53], "scene": "rain"},
            {"id": "verse-13", "surah": 40, "ayahs": [60], "scene": "ascend"},
            {"id": "verse-14", "surah": 65, "ayahs": [3], "scene": "stars"},
            {"id": "verse-15", "surah": 93, "ayahs": [3, 4, 5], "scene": "dawn"},
            {"id": "verse-16", "surah": 94, "ayahs": [5, 6], "scene": "dawn"},
            {"id": "verse-17", "surah": 94, "ayahs": [7, 8], "scene": "ascend"},
            {"id": "verse-18", "surah": 103, "ayahs": [1, 2, 3], "scene": "sand"},
            {"id": "verse-19", "surah": 108, "ayahs": [1, 2, 3], "scene": "growth"},
            {"id": "verse-20", "surah": 112, "ayahs": [1, 2, 3, 4], "scene": "stars"},
            {"id": "verse-21", "surah": 2, "ayahs": [201], "scene": "ascend"},
            {"id": "verse-22", "surah": 3, "ayahs": [8], "scene": "ascend"},
            {"id": "verse-23", "surah": 20, "ayahs": [114], "scene": "ascend"},
            {"id": "verse-24", "surah": 25, "ayahs": [74], "scene": "bokeh"},
            {"id": "verse-25", "surah": 17, "ayahs": [24], "scene": "bokeh"},
            {"id": "verse-26", "surah": 14, "ayahs": [40, 41], "scene": "bokeh"},
            {"id": "verse-27", "surah": 42, "ayahs": [28], "scene": "rain"},
            {"id": "verse-28", "surah": 50, "ayahs": [9], "scene": "rain"},
            {"id": "verse-29", "surah": 55, "ayahs": [19, 20], "scene": "ocean"},
            {"id": "verse-30", "surah": 67, "ayahs": [5], "scene": "stars"},
            {"id": "verse-31", "surah": 2, "ayahs": [255], "scene": "stars"},
            {"id": "verse-32", "surah": 3, "ayahs": [190], "scene": "stars"},
            {"id": "verse-33", "surah": 24, "ayahs": [35], "scene": "ascend"},
            {"id": "verse-34", "surah": 30, "ayahs": [21], "scene": "bokeh"},
            {"id": "verse-35", "surah": 36, "ayahs": [38, 39, 40], "scene": "stars"},
            {"id": "verse-36", "surah": 55, "ayahs": [26, 27], "scene": "sand"},
            {"id": "verse-37", "surah": 89, "ayahs": [27, 28, 29, 30], "scene": "ripple"},
            {"id": "verse-38", "surah": 2, "ayahs": [45], "scene": "dawn"},
            {"id": "verse-39", "surah": 11, "ayahs": [115], "scene": "dawn"},
            {"id": "verse-40", "surah": 3, "ayahs": [200], "scene": "dawn"},
            {"id": "verse-41", "surah": 50, "ayahs": [16], "scene": "ascend"},
            {"id": "verse-42", "surah": 55, "ayahs": [60], "scene": "growth"},
            {"id": "verse-43", "surah": 16, "ayahs": [18], "scene": "growth"},
            {"id": "verse-44", "surah": 15, "ayahs": [22], "scene": "rain"},
            {"id": "verse-45", "surah": 16, "ayahs": [14], "scene": "ocean"},
            {"id": "verse-46", "surah": 55, "ayahs": [24], "scene": "ocean"},
        ],
        "friday": [
            {"id": "friday-01", "surah": 33, "ayahs": [56], "scene": "geometric"},
            {"id": "friday-02", "surah": 62, "ayahs": [9], "scene": "geometric"},
            {"id": "friday-03", "surah": 18, "ayahs": [10], "scene": "stars"},
        ],
        "kahf": [{"id": "kahf-01", "surah": 18, "ayahs": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]}],
    },
    "references": [
        [2, 45],
        [2, 152],
        [2, 153],
        [2, 186],
        [2, 201],
        [2, 255],
        [2, 285],
        [2, 286],
        [3, 8],
        [3, 139],
        [3, 190],
        [3, 200],
        [11, 115],
        [13, 28],
        [14, 7],
        [14, 40],
        [14, 41],
        [15, 22],
        [16, 14],
        [16, 18],
        [16, 128],
        [17, 24],
        [18, 1],
        [18, 2],
        [18, 3],
        [18, 4],
        [18, 5],
        [18, 6],
        [18, 7],
        [18, 8],
        [18, 9],
        [18, 10],
        [20, 114],
        [21, 87],
        [24, 35],
        [25, 74],
        [29, 69],
        [30, 21],
        [33, 41],
        [33, 42],
        [33, 56],
        [36, 38],
        [36, 39],
        [36, 40],
        [39, 53],
        [40, 60],
        [42, 28],
        [50, 9],
        [50, 16],
        [55, 19],
        [55, 20],
        [55, 24],
        [55, 26],
        [55, 27],
        [55, 60],
        [62, 9],
        [65, 3],
        [67, 5],
        [89, 27],
        [89, 28],
        [89, 29],
        [89, 30],
        [93, 3],
        [93, 4],
        [93, 5],
        [94, 5],
        [94, 6],
        [94, 7],
        [94, 8],
        [103, 1],
        [103, 2],
        [103, 3],
        [108, 1],
        [108, 2],
        [108, 3],
        [112, 1],
        [112, 2],
        [112, 3],
        [112, 4],
        [113, 1],
        [113, 2],
        [113, 3],
        [113, 4],
        [113, 5],
        [114, 1],
        [114, 2],
        [114, 3],
        [114, 4],
        [114, 5],
        [114, 6],
    ],
}


def rows_from_json(raw: bytes) -> list[dict[str, Any]] | None:
    try:
        obj = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        return None
    rows = obj.get("data", obj) if isinstance(obj, dict) else obj
    if not isinstance(rows, list) or not rows:
        return None
    if not all(
        isinstance(row, dict)
        and {"sura_no", "aya_no"} <= row.keys()
        and isinstance(row.get("aya_text_unicode"), str)
        for row in rows
    ):
        return None
    return rows


def read_package(raw: bytes) -> dict[tuple[int, int], str]:
    digest = hashlib.sha256(raw).hexdigest().upper()
    if digest != OFFICIAL_SHA256:
        raise ValueError(f"KFGQPC SHA-256 mismatch: expected {OFFICIAL_SHA256}; got {digest}")
    rows = rows_from_json(raw)
    if rows is None:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".json")]
            names.sort(key=lambda n: not ("hafs" in n.lower() or "data" in n.lower()))
            for name in names:
                rows = rows_from_json(archive.read(name))
                if rows is not None:
                    break
    if rows is None:
        raise ValueError("Official Hafs JSON not found")
    index: dict[tuple[int, int], str] = {}
    for row in rows:
        surah, ayah = row["sura_no"], row["aya_no"]
        if type(surah) is not int or type(ayah) is not int:
            raise ValueError("Non-integer ayah coordinates")
        key = (surah, ayah)
        value = row["aya_text_unicode"]
        if not 1 <= surah <= 114 or ayah < 1 or key in index or not value:
            raise ValueError("Invalid or duplicate ayah")
        # No strip(), normalisation, punctuation, or other edits to sacred bytes.
        index[key] = value
    if len(index) != 6236 or len({s for s, _ in index}) != 114:
        raise ValueError("Expected 6236 ayahs in 114 surahs")
    return index


def build(raw: bytes, output: Path) -> None:
    index = read_package(raw)
    items = []
    for surah, ayah in MANIFEST["references"]:
        value = index[(surah, ayah)]
        items.append(
            {
                "surah": surah,
                "ayah": ayah,
                "text": value,
                "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            }
        )
    document = {
        "schema": 1,
        "language": "ar",
        "sacred": True,
        "kind": "quran_subset",
        "source_repo": "Khalidshar/status-bot@b445281",
        "zip_url": OFFICIAL_URL,
        "zip_sha256": OFFICIAL_SHA256,
        "publication_date": PUBLICATION_DATE,
        "previous_zip_sha256": PREVIOUS_SHA256,
        "checksum_note": (
            "البصمة القديمة تخص نسخة سابقة؛ اعتمد المالك بصمة إعادة النشر بتاريخ 2026-09-30."
        ),
        "package_version": "kfgqpc_hafs_v30",
        "build_date": datetime.now(UTC).date().isoformat(),
        "package_ayahs": 6236,
        "package_surahs": 114,
        "collections": MANIFEST["collections"],
        "items": items,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, help="Already downloaded official package")
    args = parser.parse_args()
    if args.zip:
        raw = args.zip.read_bytes()
    else:
        # Fixed HTTPS endpoint, one request; no retries, secrets or CI execution.
        with urlopen(OFFICIAL_URL, timeout=45) as response:
            raw = response.read()
    build(raw, OUTPUT)


if __name__ == "__main__":
    main()
