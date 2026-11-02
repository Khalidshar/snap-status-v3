"""Exact KFGQPC text + short-lived QF assets. No AI and no delivery authority."""

import hashlib
import json
import math
import re
import shutil
import sqlite3
import time
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from studio_core.network import NetworkError
from studio_core.network.quran import CDN_HOSTS, ENVIRONMENTS
from studio_core.presentation import faith
from studio_core.renderer import run, write_once

ROOT = Path(__file__).resolve().parents[4]
CATALOG = ROOT / "config/quran/reciters.json"
APPS = {"snap-bot-v2", "status-bot-v2"}
WEEK = 7 * 86400
# Delete before the hard deadline, allowing a 30-second retention worker sweep.
RETENTION = WEEK - 120
TAFSIRS = (("ar-tafsir-muyassar", "التفسير الميسر"), ("ar-tafseer-al-saddi", "تفسير السعدي"))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def exact_ayah(surah: int, ayah: int) -> str:
    # Only the independently pinned official subset is admitted. Never use QF Quran text.
    return faith.text({"surah": surah, "ayahs": [ayah]})


def safe_root(root: Path) -> Path:
    root = root.absolute()
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("QF_UNSAFE_ROOT")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


class Store:
    """A private root per bot; payload directories expire, audit/rotation never do."""

    def __init__(self, root: Path, app: str, clock: Callable[[], float] = time.time):
        if app not in APPS:
            raise ValueError("QF_APP_INVALID")
        self.root, self.app, self.clock = safe_root(root), app, clock
        self.payload = safe_root(self.root / "payload")
        self.db = self.root / "audit.sqlite"
        if self.db.is_symlink():
            raise ValueError("QF_UNSAFE_DB")
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE IF NOT EXISTS scope(app TEXT PRIMARY KEY)")
            rows = db.execute("SELECT app FROM scope").fetchall()
            if rows and rows != [(app,)]:
                raise ValueError("QF_APP_ROOT_SHARED")
            db.execute("INSERT OR IGNORE INTO scope VALUES(?)", (app,))
            db.execute(
                "CREATE TABLE IF NOT EXISTS clips(job TEXT PRIMARY KEY, reciter INTEGER NOT NULL, "
                "created REAL NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL)"
            )

    def reserve(self, job: str, catalog: dict[str, Any]) -> tuple[dict[str, Any], float]:
        if not re.fullmatch(r"[0-9a-f]{32}", job):
            raise ValueError("QF_JOB_INVALID")
        if catalog.get("schema") != 1 or catalog.get("approved_by") != "خالد":
            raise ValueError("QF_RECITERS_AWAIT_OWNER_APPROVAL")
        reciters = catalog.get("reciters")
        if catalog.get("riwayah") != "hafs" or not isinstance(reciters, list):
            raise ValueError("QF_HAFS_REQUIRED")
        if (
            not 2 <= len(reciters) <= 12
            or any(
                not isinstance(r, dict)
                or type(r.get("id")) is not int
                or r["id"] < 1
                or not isinstance(r.get("name"), str)
                or not re.fullmatch(r"[ء-ي ]+", r["name"])
                for r in reciters
            )
            or len({r["id"] for r in reciters}) != len(reciters)
        ):
            raise ValueError("QF_RECITERS_INVALID")
        with sqlite3.connect(self.db, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT reciter,expires FROM clips WHERE job=?", (job,)).fetchone()
            if prior:
                if prior[1] <= self.clock():
                    raise ValueError("QF_CLIP_EXPIRED")
                reciter = next((r for r in reciters if r["id"] == prior[0]), None)
                if reciter is None:
                    raise ValueError("QF_RESERVED_RECITER_REMOVED")
                return reciter, prior[1]
            last = db.execute("SELECT reciter FROM clips ORDER BY rowid DESC LIMIT 1").fetchone()
            index = next((i for i, r in enumerate(reciters) if last and r["id"] == last[0]), -1)
            reciter = reciters[(index + 1) % len(reciters)]
            now = self.clock()
            expires = now + RETENTION
            db.execute(
                "INSERT INTO clips VALUES(?,?,?,?,?)",
                (job, reciter["id"], now, expires, "RESERVED"),
            )
            db.commit()
        return reciter, expires

    def directory(self, job: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", job):
            raise ValueError("QF_JOB_INVALID")
        return safe_root(self.payload / job)

    def sweep(self) -> int:
        removed = 0
        with sqlite3.connect(self.db) as db:
            for (job,) in db.execute(
                "SELECT job FROM clips WHERE expires<=? AND state<>'PURGED'", (self.clock(),)
            ):
                path = self.payload / job
                if path.is_symlink():
                    raise ValueError("QF_UNSAFE_PAYLOAD")
                if path.exists():
                    shutil.rmtree(path)
                db.execute("UPDATE clips SET state='PURGED' WHERE job=?", (job,))
                removed += 1
        return removed


class LiteralHTML(HTMLParser):
    """Markup-only decoding. Visible words, spacing and punctuation are not rewritten."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"p", "div", "span", "b", "strong", "i", "em", "br"} or any(
            k not in {"class", "dir"} for k, _ in attrs
        ):
            raise ValueError("QF_TAFSIR_MARKUP_UNSUPPORTED")
        if tag in {"p", "div", "br"} and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def literal_tafsir(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 100000:
        raise ValueError("QF_TAFSIR_INVALID")
    parser = LiteralHTML()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts)
    if not re.search(r"[ء-ي]", text) or re.search(r"[A-Za-z\x00-\x08\x0b-\x1f]", text):
        raise ValueError("QF_TAFSIR_NOT_ARABIC")
    return text


def audio_duration(
    path: Path, ffmpeg: str = "/usr/bin/ffmpeg", ffprobe: str = "/usr/bin/ffprobe"
) -> float:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 32 * 1024 * 1024:
        raise ValueError("QF_AUDIO_FILE_INVALID")
    options = ["-protocol_whitelist", "file,pipe"]
    value = json.loads(
        run(
            [
                ffprobe,
                "-v",
                "error",
                *options,
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            30,
        ).stdout
    )
    streams = value.get("streams", [])
    duration = float(value.get("format", {}).get("duration", "nan"))
    if (
        len(streams) != 1
        or streams[0].get("codec_type") != "audio"
        or streams[0].get("codec_name") not in {"mp3", "aac"}
        or not math.isfinite(duration)
        or not 0.25 <= duration <= 180
    ):
        raise ValueError("QF_AUDIO_DURATION_INVALID")
    run(
        [
            ffmpeg,
            "-v",
            "error",
            "-xerror",
            "-nostdin",
            *options,
            "-i",
            str(path),
            "-f",
            "null",
            "-",
        ],
        60,
    )
    return duration


class Source:
    """All network uses the Unix egress client, never renderer-side HTTP."""

    def __init__(self, client: Any, environment: str = "prelive"):
        if environment not in ENVIRONMENTS:
            raise ValueError("QF_ENV_INVALID")
        self.client, self.environment = client, environment
        self.origin = "https://" + ENVIRONMENTS[environment][1]
        self.base = self.origin + "/content/api/v4"

    def json(self, url: str) -> dict[str, Any]:
        value = json.loads(self.client.get(url).body)
        if not isinstance(value, dict):
            raise ValueError("QF_RESPONSE_INVALID")
        return value

    def snapshot(self, group: str, resource: int) -> list[dict[str, Any]]:
        # Bootstrap each fresh request; no persistent full database or regular-API cache.
        url = self.base + "/resources/sync?bootstrap=true&resources=" + quote(f"{group}:{resource}")
        snapshots = []
        for _ in range(100):
            page = self.json(url)
            sync = page["sync"]
            for mutation in sync["mutations"]:
                if (
                    mutation.get("resource_group") != group
                    or mutation.get("resource_id") != resource
                ):
                    raise ValueError("QF_SYNC_SCOPE_CHANGED")
                if mutation.get("type") == "RESOURCE_CREATE":
                    path = mutation.get("snapshot_url")
                    expected = f"/api/v4/resources/snapshots/{group}/{resource}"
                    if path != expected:
                        raise ValueError("QF_SNAPSHOT_PATH_INVALID")
                    snapshots.append(self.json(self.origin + "/content" + path))
            if sync.get("has_more") is False:
                break
            path = sync.get("next_page_url", "")
            if not isinstance(path, str) or not path.startswith("/api/v4/resources/sync?"):
                raise ValueError("QF_SYNC_PATH_INVALID")
            url = self.origin + "/content" + path
        else:
            raise ValueError("QF_SYNC_PAGE_LIMIT")
        if len(snapshots) != 1:
            raise ValueError("QF_RESOURCE_UNAVAILABLE")
        data = snapshots[0]
        if (
            data.get("resource_group") != group
            or data.get("resource_id") != resource
            or data.get("schema_version") != 1
            or not isinstance(data.get("records"), list)
        ):
            raise ValueError("QF_SNAPSHOT_INVALID")
        return list(data["records"])

    def tafsir(self, verse: str) -> dict[str, Any]:
        resources = self.json(self.base + "/resources/tafsirs?language=ar")["tafsirs"]
        for slug, name in TAFSIRS:
            matches = [
                r for r in resources if r.get("slug") == slug and r.get("language_name") == "arabic"
            ]
            if len(matches) != 1:
                continue
            resource = matches[0]["id"]
            try:
                records = self.snapshot("tafsirs", resource)
            except NetworkError:
                # An unavailable primary may use the approved secondary only.
                continue
            except ValueError as exc:
                if str(exc) != "QF_RESOURCE_UNAVAILABLE":
                    raise
                continue
            records = [r for r in records if r.get("verse_key") == verse]
            if not records:
                continue
            if len(records) != 1:
                raise ValueError("QF_TAFSIR_AMBIGUOUS")
            row = records[0]
            # Do not excerpt a group of ayahs or mislabel it as a single-ayah tafsir.
            if (
                row.get("group_verse_key_from", verse) != verse
                or row.get("group_verse_key_to", verse) != verse
            ):
                raise ValueError("QF_TAFSIR_GROUP_REQUIRES_WHOLE_CONTEXT")
            raw = row["text"]
            text = literal_tafsir(raw)
            return {
                "resource_id": resource,
                "name": name,
                "raw": raw,
                "text": text,
                "raw_sha256": sha(raw.encode()),
                "sha256": sha(text.encode()),
            }
        raise ValueError("QF_TAFSIR_UNAVAILABLE")

    def audio(self, verse: str, reciter: dict[str, Any]) -> bytes:
        rows = self.json(self.base + "/resources/recitations?language=ar")["recitations"]
        matches = [r for r in rows if r.get("id") == reciter["id"]]
        if (
            len(matches) != 1
            or matches[0].get("translated_name", {}).get("name") != reciter["name"]
        ):
            raise ValueError("QF_RECITER_IDENTITY_UNVERIFIED")
        rows = self.snapshot("recitations", reciter["id"])
        files = [
            r
            for r in rows
            if r.get("record_type") == "audio_file"
            and r.get("verse_key") == verse
            and r.get("recitation_id") == reciter["id"]
        ]
        if len(files) != 1:
            raise ValueError("QF_AUDIO_UNAVAILABLE")
        url = files[0]["url"]
        if isinstance(url, str) and re.fullmatch(r"/?[A-Za-z0-9_/-]+\.mp3", url):
            if ".." in url:
                raise ValueError("QF_AUDIO_URL_DENIED")
            url = "https://verses.quran.com/" + url.lstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in CDN_HOSTS
            or parsed.query
            or parsed.username
            or parsed.password
            or not parsed.path.endswith(".mp3")
        ):
            raise ValueError("QF_AUDIO_URL_DENIED")
        return bytes(self.client.get(url).body)


def prepare(
    store: Store, source: Source, job: str, surah: int, ayah: int, catalog: Path = CATALOG
) -> Path:
    store.sweep()
    text = exact_ayah(surah, ayah)
    if source.environment == "prelive" and surah not in {1, 2}:
        raise ValueError("QF_PRELIVE_SURAH_UNAVAILABLE")
    reciter, expires = store.reserve(job, json.loads(catalog.read_text()))
    directory = store.directory(job)
    target = directory / "bundle.json"
    if target.exists():
        verify_bundle(target, store.app, store.clock())
        prior = json.loads(target.read_bytes())
        if (prior["surah"], prior["ayah"]) != (surah, ayah):
            raise ValueError("QF_JOB_CONTENT_CHANGED")
        return target
    verse = f"{surah}:{ayah}"
    tafsir = source.tafsir(verse)  # Missing tafsir blocks, never substitute generated text.
    audio = directory / "recitation.mp3"
    duration = None
    try:
        write_once(audio, source.audio(verse, reciter))
        duration = audio_duration(audio)
        audio_state = "HUMAN_RECITATION"
    except Exception:
        # Only the audio degrades to silence. No TTS, music or another reader.
        audio.unlink(missing_ok=True)
        audio_state = "SILENT_AUDIO_FAILED"
    value = {
        "schema": 1,
        "app": store.app,
        "job": job,
        "environment": source.environment,
        "surah": surah,
        "ayah": ayah,
        "text": text,
        "text_sha256": sha(text.encode()),
        "reciter": reciter,
        "tafsir": tafsir,
        "expires_at": expires,
        "audio_state": audio_state,
        "audio_duration": duration,
        "audio_sha256": sha(audio.read_bytes()) if duration else None,
        "attribution": f"التلاوة: {reciter['name']} — المصدر: مؤسسة القرآن",
    }
    if store.clock() >= expires:
        store.sweep()
        raise ValueError("QF_CLIP_EXPIRED")
    write_once(target, json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
    with sqlite3.connect(store.db) as db:
        db.execute("UPDATE clips SET state=? WHERE job=?", (audio_state, job))
    return target


def verify_bundle(path: Path, app: str, now: float) -> dict[str, Any]:
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("QF_UNSAFE_BUNDLE")
    value: dict[str, Any] = json.loads(path.read_bytes())
    if app not in APPS or value.get("schema") != 1 or value.get("app") != app:
        raise ValueError("QF_BUNDLE_SCOPE_CHANGED")
    expires: Any = value.get("expires_at")
    if (
        type(expires) not in {int, float}
        or not math.isfinite(expires)
        or not now < expires <= now + RETENTION
    ):
        raise ValueError("QF_CLIP_EXPIRED")
    text = exact_ayah(value["surah"], value["ayah"])
    tafsir = value["tafsir"]
    if (
        value["text"] != text
        or value["text_sha256"] != sha(text.encode())
        or tafsir["name"] not in {n for _, n in TAFSIRS}
        or tafsir["sha256"] != sha(tafsir["text"].encode())
        or tafsir["raw_sha256"] != sha(tafsir["raw"].encode())
        or literal_tafsir(tafsir["raw"]) != tafsir["text"]
    ):
        raise ValueError("QF_TEXT_CHANGED")
    if value["attribution"] != f"التلاوة: {value['reciter']['name']} — المصدر: مؤسسة القرآن":
        raise ValueError("QF_ATTRIBUTION_CHANGED")
    if value["audio_state"] == "HUMAN_RECITATION":
        audio = path.parent / "recitation.mp3"
        if audio.is_symlink():
            raise ValueError("QF_UNSAFE_AUDIO")
        if (
            sha(audio.read_bytes()) != value["audio_sha256"]
            or abs(audio_duration(audio) - value["audio_duration"]) > 0.05
        ):
            raise ValueError("QF_AUDIO_CHANGED")
    elif (
        value["audio_state"] != "SILENT_AUDIO_FAILED"
        or value["audio_sha256"] is not None
        or value["audio_duration"] is not None
    ):
        raise ValueError("QF_AUDIO_POLICY_INVALID")
    return value
