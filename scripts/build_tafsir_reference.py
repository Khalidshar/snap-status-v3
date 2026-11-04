"""Build config/quran/tafsir-reference.json: reviewed hashes of the Muyassar tafsir.

Owner decision (4 Nov 2026): every verse the bots may show gets the SHA-256 of the
Quran Foundation tafsir text, recorded only when that text is identical to the same
verse in the independent QuranEnc «arabic_moyassar» edition, as the bot would display it
(quran_foundation.literal_tafsir). A difference, or markup the bot refuses to display
(QF's <span class="blue">), is listed and left out, so that verse's QF tafsir stays off
until a person reviews it. The tafsir text is never edited or stored here, only its hash.

    PYTHONPATH=packages/studio-core python scripts/build_tafsir_reference.py > ref.json
"""

import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QF = "https://api.quran.com/api/v4/tafsirs/16/by_ayah/{}"
QURANENC = "https://quranenc.com/api/v1/translation/aya/arabic_moyassar/{}/{}"
AGENT = "madar-tafsir-reference/1"


def fetch(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https hosts
        return json.loads(response.read())


def verses() -> list[str]:
    keys = set()
    subset = json.loads(
        (
            ROOT / "packages/studio-core/studio_core/presentation/data/faith/quran_subset.json"
        ).read_text(encoding="utf-8")
    )
    keys |= {f"{i['surah']}:{i['ayah']}" for i in subset["items"]}
    data = ROOT / "packages/studio-core/studio_core/production/data"
    if (data / "status.json").exists():
        pools = json.loads((data / "status.json").read_text(encoding="utf-8"))
        for name in ("verses", "friday_verses", "kahf_verses", "fast_verses", "isha_verses"):
            keys |= {f"{s}:{a}" for s, ayahs, _ in pools.get(name, []) for a in ayahs}
    if (data / "passages.json").exists():
        for p in json.loads((data / "passages.json").read_text(encoding="utf-8")):
            keys |= {f"{p['s']}:{a}" for a in p["a"]}
    return sorted(keys, key=lambda k: tuple(int(x) for x in k.split(":")))


def main() -> int:
    from studio_core.religious.quran_foundation import literal_tafsir

    hashes, differs = {}, []
    for key in verses():
        surah, ayah = key.split(":")
        qf = fetch(QF.format(key))["tafsir"]
        if qf.get("resource_id") != 16:
            raise SystemExit("QF resource changed")
        independent = fetch(QURANENC.format(surah, ayah))["result"]["translation"]
        try:
            shown = literal_tafsir(qf["text"])
        except ValueError:
            shown = None
        if shown is not None and shown.strip() == independent.strip():
            hashes[key] = hashlib.sha256(qf["text"].encode()).hexdigest()
        else:
            differs.append(key)
        time.sleep(0.2)
    json.dump(
        {
            "schema": 1,
            "reviewed": "2026-11-04",
            "method": "QF text, markup removed as displayed, identical to QuranEnc arabic_moyassar; "
            "hash of the raw QF text only",
            "sources": {"ar-tafsir-muyassar": {"resource_id": 16, "verses": hashes}},
            "not_matching": differs,
        },
        sys.stdout,
        ensure_ascii=False,
        indent=1,
    )
    print(file=sys.stderr)
    print(f"verses={len(hashes)} differ={len(differs)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
