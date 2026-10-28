"""Manually bundle the exact approved font. CI never downloads the package."""

import argparse
import hashlib
import io
import json
import zipfile
from pathlib import Path
from urllib.request import urlopen

SOURCE_ZIP = "https://download.qurancomplex.gov.sa/resources_dev/kfgqpc_hafs_v30.zip"
ZIP_SHA256 = "227E6B1564D980F2BD09C2C35EBFB0330AC268C79A7C247CD1AB665BC635F245"
MEMBER = "kfgqpc_hafs_v30-font/kfgqpc_hafs_v30.ttf"
FAMILY = "kfgqpc_hafs_v3"
OUTPUT = Path(__file__).resolve().parents[1] / "assets/fonts/kfgqpc"
LICENSE_NOTE = (
    "اتفاقية المستخدم النهائي مضمّنة في جدول name بالخط الرسمي، السجل nameID 13. "
    "تتيح الاستخدام والنسخ والتوزيع دون تكلفة بشروطها، وتحظر البيع والتعديل والتحويل "
    "والهندسة العكسية. حقوق الملكية للمجمع، والخط مقدم دون ضمان. "
    "الرابط يشير إلى الحزمة الرسمية المحتوية على الاتفاقية؛ لم يُفترض ترخيص مفتوح آخر."
)


def extract(raw: bytes, output: Path) -> None:
    """Verify before opening; read just the named member, never rewrite font bytes."""
    if hashlib.sha256(raw).hexdigest().upper() != ZIP_SHA256:
        raise ValueError("Unapproved Quran font archive SHA-256")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        font = archive.read(MEMBER)
    manifest = {
        "schema": 1,
        "family": FAMILY,
        "file": "kfgqpc_hafs_v30.ttf",
        "sha256": hashlib.sha256(font).hexdigest(),
        "source_zip": SOURCE_ZIP,
        "source_zip_sha256": ZIP_SHA256,
        "publication_date": "2026-09-30",
        "license_url": SOURCE_ZIP,
        "license_note": LICENSE_NOTE,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / manifest["file"]).write_bytes(font)
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, help="Reuse an already downloaded official package")
    args = parser.parse_args()
    if args.zip:
        raw = args.zip.read_bytes()
    else:
        # One request to the fixed official HTTPS endpoint, no retries or secrets.
        with urlopen(SOURCE_ZIP, timeout=45) as response:
            raw = response.read()
    extract(raw, OUTPUT)


if __name__ == "__main__":
    main()
