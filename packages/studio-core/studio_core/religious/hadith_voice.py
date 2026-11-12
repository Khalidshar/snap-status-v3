"""Owner-approved stored audio, bound to the exact UTF-8 hadith text and file bytes."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4] / "assets/voice/hadith"


def entry(identifier: str, text: str, *, root: Path | None = None) -> dict[str, Any]:
    root = ROOT if root is None else root
    if not re.fullmatch(r"[a-z0-9_-]{1,100}", identifier):
        raise ValueError("HADITH_VOICE_ID_INVALID")
    manifest = root / "manifest.json"
    if any(p.is_symlink() for p in (manifest, *manifest.parents)):
        raise ValueError("HADITH_VOICE_UNSAFE")
    try:
        with manifest.open("rb") as stream:
            raw = stream.read(200_001)
        if len(raw) > 200_000:
            raise ValueError("HADITH_VOICE_MANIFEST_INVALID")
        data = json.loads(raw)
    except (OSError, ValueError) as error:
        raise ValueError("HADITH_VOICE_UNAVAILABLE") from error
    if (
        not isinstance(data, dict)
        or type(data.get("schema")) is not int
        or data.get("schema") != 1
        or not isinstance(data.get("recordings"), list)
        or len(data["recordings"]) > 366
    ):
        raise ValueError("HADITH_VOICE_MANIFEST_INVALID")
    entries = data["recordings"]
    ids = [e.get("id") for e in entries if isinstance(e, dict)]
    if (
        len(ids) != len(entries)
        or any(not isinstance(i, str) for i in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("HADITH_VOICE_MANIFEST_INVALID")
    row = next((e for e in entries if e["id"] == identifier), None)
    if row is None or row.get("approved") is not True:
        raise ValueError("HADITH_VOICE_UNAVAILABLE")
    if (
        set(row) != {"id", "approved", "text_sha256", "audio_sha256"}
        or row["text_sha256"] != hashlib.sha256(text.encode("utf-8")).hexdigest()
        or not isinstance(row["audio_sha256"], str)
        or not re.fullmatch("[0-9a-f]{64}", row["audio_sha256"])
    ):
        raise ValueError("HADITH_VOICE_TEXT_OR_HASH_CHANGED")
    return dict(row)


def read(identifier: str, text: str, *, root: Path | None = None) -> bytes:
    root = ROOT if root is None else root
    proof = entry(identifier, text, root=root)
    path = root / (identifier + ".mp3")
    if path.is_symlink():
        raise ValueError("HADITH_VOICE_UNSAFE")
    try:
        with path.open("rb") as stream:
            raw = stream.read(8_000_001)
    except OSError as error:
        raise ValueError("HADITH_VOICE_UNAVAILABLE") from error
    if (
        not 0 < len(raw) <= 8_000_000
        or hashlib.sha256(raw).hexdigest() != proof["audio_sha256"]
        or not (raw.startswith(b"ID3") or (len(raw) >= 2 and raw[0] == 255 and raw[1] & 224 == 224))
    ):
        raise ValueError("HADITH_VOICE_FILE_CHANGED")
    return raw
