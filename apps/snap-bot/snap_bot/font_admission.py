"""Repository-pinned font and license admission. No font discovery or network."""

import hashlib
import json
import os
import stat
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MANIFEST_SHA256 = "9c6d55bf81b0928a03c18fc01b1ca3f28c931e64222f3e8774760fba91cf80e0"


def verified_file(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Unsafe font/license path")
    fd = (
        os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        if hasattr(os, "O_NOFOLLOW")
        else os.open(path, os.O_RDONLY)
    )
    with os.fdopen(fd, "rb") as source:
        meta = os.fstat(source.fileno())
        if not stat.S_ISREG(meta.st_mode) or not 0 < meta.st_size <= 5_000_000:
            raise ValueError("Unsafe font/license file")
        return source.read()


def registry(profile):
    if profile.get("font_manifest_sha256") != MANIFEST_SHA256:
        raise ValueError("Unknown profile font registry")
    path = Path(profile.get("font_manifest", "assets/fonts/manifest.json"))
    if not path.is_absolute():
        path = REPO / path
    raw = verified_file(path)
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise ValueError("Unknown font registry")
    value = json.loads(raw)
    expected = {entry["file"]: entry["sha256"] for entry in value["fonts"]}
    if profile.get("font_files") != expected:
        raise ValueError("Font profile inventory mismatch")
    return path.parent, value


def admit_font(path, expected_sha, profile, *, latin=False):
    root, value = registry(profile)
    path = Path(path)
    if not path.is_absolute():
        path = root.parent.parent / path
    data = verified_file(path)
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha:
        raise ValueError("Latin font version mismatch" if latin else "Font version mismatch")
    entry = next((entry for entry in value["fonts"] if entry["sha256"] == digest), None)
    if entry is None:
        raise ValueError("Unknown font fingerprint")
    if not entry.get("licenses"):
        raise ValueError("Missing font license")
    for license in entry["licenses"]:
        license_path = root / license["file"]
        if hashlib.sha256(verified_file(license_path)).hexdigest() != license["sha256"]:
            raise ValueError("Font license mismatch")
    return path


def font_paths(profile):
    return (
        admit_font(profile["font"], profile["font_sha256"], profile),
        admit_font(profile["latin_font"], profile["latin_font_sha256"], profile, latin=True),
    )


def fallback_fonts(profile):
    result = []
    for file in profile.get("font_fallbacks", []):
        key = file.removeprefix("assets/fonts/")
        if key not in profile["font_files"]:
            raise ValueError("Unknown fallback font")
        result.append(admit_font(file, profile["font_files"][key], profile))
    return result


def choose_font(text, preferred, charsets, fallbacks, check):
    for font in dict.fromkeys([preferred, *fallbacks]):
        try:
            check(text, charsets[font])
        except ValueError:
            continue
        return font
    raise ValueError("Font missing required glyph")
