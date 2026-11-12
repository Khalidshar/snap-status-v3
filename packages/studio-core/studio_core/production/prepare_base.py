# ruff: noqa: E501
"""Shared source access, asset storage and history for production preparation."""

import html
import json
import math
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlencode, urljoin, urlsplit

from studio_core.config import Config
from studio_core.network import NetworkClient
from studio_core.network.production import ProductionPolicy
from studio_core.renderer import run

from .model import (
    NEWS,
    TITLES,
    sha,
)

DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def cut_matches(seconds: float, references: list[float]) -> bool:
    """A cut ayah is believed only near the median length of the same ayah elsewhere.

    Reciters differ in pace (Maher 42 s and Minshawy 68 s for Ayat al-Kursi), so the window
    is wide; it still refuses a neighbouring ayah (2:255 is 55 s, 2:256 is 25 s).
    """
    if not references:
        return False  # no reference, no belief
    middle = sorted(references)[len(references) // 2]
    return 0.6 * middle <= seconds <= 1.6 * middle


def duration(path: Path) -> float:
    result = run(
        [
            "/usr/bin/ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        30,
    )
    value = float(result.stdout.strip())
    if not math.isfinite(value) or value <= 0:
        raise ValueError("LEGACY_AUDIO_DURATION_INVALID")
    return value


def safe_xml(raw: bytes) -> ET.Element:
    if len(raw) > 4_000_000 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("LEGACY_XML_UNSAFE")
    return ET.fromstring(raw)  # noqa: S314 — entities/DOCTYPE rejected above


def network_client(config: Config) -> NetworkClient:
    # Preserve the public dependency seam used by existing callers and tests.
    from . import prepare

    return prepare.NetworkClient(config)


class PreparationBase:
    def __init__(
        self,
        app: str,
        key: str,
        job: str,
        root: Path,
        *,
        client: Any = None,
        state: Path | None = None,
    ) -> None:
        if (
            app not in {"snap-bot-v2", "status-bot-v2"}
            or key not in TITLES
            or not re.fullmatch("[a-f0-9]{32}", job)
        ):
            raise ValueError("LEGACY_FACTORY_SCOPE")
        self.app, self.key, self.job, self.root = app, key, job, root
        short = "snap" if app == "snap-bot-v2" else "status"
        self.client = client or network_client(
            cast(Config, ProductionPolicy(Path(f"/run/madar/{short}-production.sock")))
        )
        self.assets: list[dict[str, Any]] = []
        self.audio: list[dict[str, Any]] = []
        self.sources: list[dict[str, Any]] = []
        self.translations: dict[str, str] = {}
        self.root.mkdir(parents=True, exist_ok=False, mode=0o700)
        (root / "assets").mkdir(mode=0o750)
        folder = state or Path(
            os.getenv("MADAR_PRODUCTION_STATE_ROOT", f"/var/lib/madar/{short}/production")
        )
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = folder / "history.sqlite"
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE IF NOT EXISTS scope(app TEXT PRIMARY KEY)")
            rows = list(db.execute("SELECT app FROM scope"))
            if rows and rows != [(app,)]:
                raise ValueError("LEGACY_HISTORY_SCOPE")
            db.execute("INSERT OR IGNORE INTO scope VALUES(?)", (app,))
            db.execute(
                "CREATE TABLE IF NOT EXISTS used(kind TEXT, fingerprint TEXT, job TEXT, at REAL, PRIMARY KEY(kind,fingerprint))"
            )
            db.execute("CREATE TABLE IF NOT EXISTS readers(job TEXT PRIMARY KEY, id TEXT, at REAL)")

    def get(self, url: str) -> bytes:
        response = self.client.get(quote(url, safe=":/?&=+%-._~"))
        if response.status != 200:
            raise ValueError("LEGACY_SOURCE_UNAVAILABLE")
        return bytes(response.body)

    def json(self, url: str) -> Any:
        raw = self.get(url)
        self.save(raw, ".json", url, "source response")
        return json.loads(
            raw.decode("utf-8-sig"),
            strict=urlsplit(url).hostname not in {"hisnmuslim.com", "www.hisnmuslim.com"},
        )

    def save(self, raw: bytes, suffix: str, url: str, credit: str) -> str:
        # A running number, never reused: dropped assets must not hand their name to a new one.
        serial = self.__dict__.get("_serial", len(self.assets))
        self._serial = serial + 1
        name = f"assets/{serial:03d}{suffix}"
        path = self.root / name
        path.write_bytes(raw)
        path.chmod(0o640)
        self.assets.append({"file": name, "sha256": sha(raw), "source_url": url, "credit": credit})
        return name

    def local(self, path: Path, source: str, credit: str) -> str:
        return self.save(path.read_bytes(), path.suffix, source, credit)

    def used(self, fingerprint: str, *, reserve: bool = True) -> bool:
        window = 4 * 86400 if self.key in NEWS | {"tech", "style"} else 30 * 86400
        with sqlite3.connect(self.db) as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT job,at FROM used WHERE kind=? AND fingerprint=?", (self.key, fingerprint)
            ).fetchone()
            if old and old[0] != self.job and old[1] > time.time() - window:
                return True
            if reserve:
                db.execute(
                    "INSERT OR REPLACE INTO used VALUES(?,?,?,?)",
                    (self.key, fingerprint, self.job, time.time()),
                )
        return False

    def picture(self, url: str, base: str, credit: str) -> str:
        url = urljoin(base, html.unescape(url))
        raw = self.get(url)
        if not (
            raw.startswith(b"\xff\xd8")
            or raw.startswith(b"\x89PNG")
            or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")
        ):
            raise ValueError("LEGACY_IMAGE_TYPE_INVALID")
        original = self.root / "source-image.img"
        original.write_bytes(raw)
        normalized = self.root / "image.jpg"
        run(
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(original),
                "-frames:v",
                "1",
                "-vf",
                "scale=1200:1200:force_original_aspect_ratio=decrease",
                str(normalized),
            ],
            45,
        )
        return self.local(normalized, url, credit)

    def translate(self, text: str) -> str:
        from .translation import decode, request_url

        if not text:
            return text
        if self.key not in {"tech", "style"}:
            raise ValueError("TRANSLATION_SACRED_OR_NEWS_FORBIDDEN")
        url = request_url(text)
        raw = self.get(url)
        target = decode(text, raw)
        proof = self.save(raw, ".json", url, "ترجمة آلية مباشرة من النص الإنجليزي الأصلي")
        self.translations[text] = proof
        return target

    def spoken(self, text: str, kind: str) -> tuple[str, float]:
        url = "tts://" + kind + "?" + urlencode({"text": text})
        path = self.save(
            self.get(url), ".mp3", url, "Microsoft Edge — ar-SA-HamedNeural (old free voice)"
        )
        return path, duration(self.root / path)
