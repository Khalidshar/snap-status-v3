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
TAFSIR_REFERENCE = ROOT / "config/quran/tafsir-reference.json"
APPS = {"snap-bot-v2", "status-bot-v2"}
WEEK = 7 * 86400
# Delete before the hard deadline; also refuse expired assets before every use.
RETENTION = WEEK - 120
TAFSIRS = (("ar-tafsir-muyassar", "التفسير الميسر"), ("ar-tafseer-al-saddi", "تفسير السعدي"))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_tafsir_reference(verse: str, tafsir: dict[str, Any]) -> None:
    """Check the selected source and verse against independently reviewed hashes."""
    try:
        reference = json.loads(TAFSIR_REFERENCE.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError("QF_TAFSIR_REFERENCE_UNAVAILABLE") from exc
    if not isinstance(reference, dict) or reference.get("schema") != 1:
        raise ValueError("QF_TAFSIR_REFERENCE_INVALID")
    sources = reference.get("sources")
    if not isinstance(sources, dict):
        raise ValueError("QF_TAFSIR_REFERENCE_INVALID")
    slug = next((slug for slug, name in TAFSIRS if name == tafsir["name"]), None)
    source = sources.get(slug)
    if not isinstance(source, dict):
        raise ValueError("QF_TAFSIR_REFERENCE_UNAVAILABLE")
    verses = source.get("verses")
    if not isinstance(verses, dict) or source.get("resource_id") != tafsir["resource_id"]:
        raise ValueError("QF_TAFSIR_REFERENCE_INVALID")
    expected = verses.get(verse)
    if expected is None:
        raise ValueError("QF_TAFSIR_REFERENCE_UNAVAILABLE")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("QF_TAFSIR_REFERENCE_INVALID")
    if sha(tafsir["raw"].encode()) != expected:
        raise ValueError("QF_TAFSIR_REFERENCE_CHANGED")


def valid_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 80
        and re.fullmatch(r"[\wً-ْـ .'()-]+", value) is not None
        and not value.isdigit()
    )


def reciter_name(row: dict[str, Any]) -> str:
    """Arabic name as QF publishes it; QF's own reciter name when no Arabic translation."""
    translated = row.get("translated_name") or {}
    name = translated.get("name")
    if isinstance(name, str) and re.search(r"[ء-ي]", name):
        return name.strip()
    return str(row.get("reciter_name", "")).strip()


def eligible(rows: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any]:
    """Every QF recitation that meets the owner policy, in stable id order.

    Owner decision 2 Nov: never a fixed list; new QF reciters join automatically.
    """
    if policy.get("schema") != 2 or policy.get("riwayah") != "hafs":
        raise ValueError("QF_RECITER_POLICY_INVALID")
    styles = set(policy["exclude_styles"])
    markers = [m.casefold() for m in policy["exclude_name_markers"]]
    chosen: dict[int, dict[str, Any]] = {}
    for row in rows:
        identifier, name = row.get("id"), reciter_name(row)
        text = (name + " " + str(row.get("reciter_name", ""))).casefold()
        if (
            type(identifier) is not int
            or identifier < 1
            or not valid_name(name)
            or str(row.get("style") or "").casefold() in styles
            or any(m in text for m in markers)
        ):
            continue
        chosen.setdefault(identifier, {"id": identifier, "name": name})
    if not chosen:
        raise ValueError("QF_RECITERS_UNAVAILABLE")
    return {"reciters": [chosen[k] for k in sorted(chosen)]}


def exact_ayah(surah: int, ayah: int) -> str:
    # Only the independently pinned official subset is admitted. Never use QF Quran text.
    return faith.text({"surah": surah, "ayahs": [ayah]})


def safe_root(root: Path) -> Path:
    root = root.absolute()
    if ".." in root.parts or any(p.is_symlink() for p in (root, *root.parents)):
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
            db.execute(
                "CREATE TABLE IF NOT EXISTS copies(path TEXT PRIMARY KEY, job TEXT NOT NULL, "
                "expires REAL NOT NULL, sha TEXT NOT NULL, state TEXT NOT NULL)"
            )

    def copy_path(self, path: Path) -> Path:
        path = path.absolute()
        if ".." in path.parts:
            raise ValueError("QF_COPY_OUTSIDE_APP")
        short = "snap" if self.app == "snap-bot-v2" else "status"
        app_root = self.root.parent / short
        temporary = any(
            re.fullmatch(r"madar-manual-[A-Za-z0-9_-]+", part) for part in path.parts
        ) and path.is_relative_to(Path("/tmp"))  # noqa: S108 -- owned temporary workspaces only
        coordinator = short == "snap" and path.is_relative_to(self.root.parent / "render-work")
        if not (path.is_relative_to(app_root) or temporary or coordinator):
            raise ValueError("QF_COPY_OUTSIDE_APP")
        asset_copy = path.parent.name == "assets" and bool(
            re.fullmatch(r"[0-9]{3}\.mp3", path.name)
        )
        if (
            not asset_copy
            and path.name
            not in {"did-you-know.mp4", "preview.png", "video.mp4", "clip.mp4", "mix.wav"}
            and not re.fullmatch(r"\.adopt-[A-Za-z0-9_-]+", path.name)
        ):
            raise ValueError("QF_COPY_TYPE_DENIED")
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("QF_UNSAFE_COPY")
        return path

    def track(self, path: Path, job: str, expires: float, digest: str) -> None:
        path = self.copy_path(path)
        with sqlite3.connect(self.db) as db:
            row = db.execute("SELECT expires FROM clips WHERE job=?", (job,)).fetchone()
            if not row or expires != row[0] or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("QF_COPY_BINDING_INVALID")
            prior = db.execute(
                "SELECT job,expires,sha FROM copies WHERE path=?", (str(path),)
            ).fetchone()
            if prior and prior != (job, expires, digest):
                raise ValueError("QF_COPY_BINDING_CHANGED")
            db.execute(
                "INSERT OR IGNORE INTO copies VALUES(?,?,?,?,'LIVE')",
                (str(path), job, expires, digest),
            )

    def reserve(self, job: str, catalog: dict[str, Any]) -> tuple[dict[str, Any], float]:
        if not re.fullmatch(r"[0-9a-f]{32}", job):
            raise ValueError("QF_JOB_INVALID")
        reciters = catalog.get("reciters")
        if not isinstance(reciters, list) or not 1 <= len(reciters) <= 500:
            raise ValueError("QF_RECITERS_UNAVAILABLE")
        if any(
            not isinstance(r, dict)
            or type(r.get("id")) is not int
            or r["id"] < 1
            or not valid_name(r.get("name"))
            for r in reciters
        ) or len({r["id"] for r in reciters}) != len(reciters):
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
            last = db.execute(
                "SELECT reciter FROM clips WHERE state NOT IN ('FAILED','PURGED_FAILED') "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
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

    def failed(self, job: str) -> None:
        with sqlite3.connect(self.db) as db:
            db.execute(
                "UPDATE clips SET state='FAILED' WHERE job=? AND state NOT IN "
                "('RENDERED','PURGED','PURGED_FAILED')",
                (job,),
            )

    def rendered(self, job: str) -> None:
        with sqlite3.connect(self.db) as db:
            db.execute(
                "UPDATE clips SET state='RENDERED' WHERE job=? AND expires>?", (job, self.clock())
            )

    def directory(self, job: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", job):
            raise ValueError("QF_JOB_INVALID")
        return safe_root(self.payload / job)

    def sweep(self) -> int:
        removed = 0
        with sqlite3.connect(self.db) as db:
            for name, digest in db.execute(
                "SELECT path,sha FROM copies WHERE expires<=? AND state='LIVE'", (self.clock(),)
            ).fetchall():
                path = self.copy_path(Path(name))
                state = "PURGED"
                if path.exists():
                    if not path.is_file():
                        raise ValueError("QF_UNSAFE_COPY")
                    if sha(path.read_bytes()) != digest:
                        # An interrupted copy may be partial. Its uniquely registered
                        # app-local path still expires; keep the discrepancy in audit.
                        state = "PURGED_CHANGED"
                    path.unlink()
                db.execute("UPDATE copies SET state=? WHERE path=?", (state, name))
            for (job,) in db.execute(
                "SELECT job FROM clips WHERE expires<=? "
                "AND state NOT IN ('PURGED','PURGED_FAILED')",
                (self.clock(),),
            ):
                path = self.payload / job
                if path.is_symlink():
                    raise ValueError("QF_UNSAFE_PAYLOAD")
                if path.exists():
                    shutil.rmtree(path)
                db.execute(
                    "UPDATE clips SET state=CASE WHEN state='FAILED' THEN 'PURGED_FAILED' "
                    "ELSE 'PURGED' END WHERE job=?",
                    (job,),
                )
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
            tafsir = {
                "resource_id": resource,
                "name": name,
                "raw": raw,
                "text": text,
                "raw_sha256": sha(raw.encode()),
                "sha256": sha(text.encode()),
            }
            verify_tafsir_reference(verse, tafsir)
            return tafsir
        raise ValueError("QF_TAFSIR_UNAVAILABLE")

    def recitations(self) -> list[dict[str, Any]]:
        rows = self.json(self.base + "/resources/recitations?language=ar")["recitations"]
        if not isinstance(rows, list):
            raise ValueError("QF_RESPONSE_INVALID")
        return [r for r in rows if isinstance(r, dict)]

    def catalog(self, policy: dict[str, Any]) -> dict[str, Any]:
        return eligible(self.recitations(), policy)

    def recitation_rows(self, reciter_id: int) -> list[dict[str, Any]]:
        """One snapshot per reciter per request (audio files and their word timings)."""
        cache = self.__dict__.setdefault("_rows", {})
        if reciter_id not in cache:
            cache[reciter_id] = self.snapshot("recitations", reciter_id)
        return list(cache[reciter_id])

    def segments(self, verse: str, reciter: dict[str, Any]) -> list[list[int]]:
        """Word timings of one ayah: [word index, word index + 1, start ms, end ms].

        Owner decision 8 Nov 2026 («تلاوة»): the shown phrase follows the reciter word by word.
        """
        rows = [
            r
            for r in self.recitation_rows(reciter["id"])
            if r.get("record_type") == "audio_file"
            and r.get("verse_key") == verse
            and r.get("recitation_id") == reciter["id"]
        ]
        if len(rows) != 1 or not isinstance(rows[0].get("segments"), list):
            raise ValueError("QF_WORD_TIMING_UNAVAILABLE")
        out = []
        for item in rows[0]["segments"]:
            word, end_word, start, stop = (int(x) for x in item[:4])
            if end_word != word + 1 or start < 0 or stop < start:
                raise ValueError("QF_WORD_TIMING_INVALID")
            out.append([word, end_word, start, stop])
        if [s[0] for s in out] != list(range(len(out))):
            raise ValueError("QF_WORD_TIMING_INVALID")
        return out

    def audio(self, verse: str, reciter: dict[str, Any]) -> bytes:
        matches = [r for r in self.recitations() if r.get("id") == reciter["id"]]
        if len(matches) != 1 or reciter_name(matches[0]) != reciter["name"]:
            raise ValueError("QF_RECITER_IDENTITY_UNVERIFIED")
        rows = self.recitation_rows(reciter["id"])
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
    exact_ayah(surah, ayah)
    if source.environment == "prelive" and surah not in {1, 2}:
        raise ValueError("QF_PRELIVE_SURAH_UNAVAILABLE")
    reciter, expires = store.reserve(job, source.catalog(json.loads(catalog.read_text())))
    directory = store.directory(job)
    try:
        return download_ayah(store, source, job, surah, ayah, reciter, expires, directory)
    except Exception:
        store.failed(job)
        raise


def download_ayah(
    store: Store,
    source: Source,
    job: str,
    surah: int,
    ayah: int,
    reciter: dict[str, Any],
    expires: float,
    directory: Path,
) -> Path:
    store.sweep()
    text = exact_ayah(surah, ayah)
    if source.environment == "prelive" and surah not in {1, 2}:
        raise ValueError("QF_PRELIVE_SURAH_UNAVAILABLE")
    directory = safe_root(directory)
    target = directory / "bundle.json"
    if target.exists():
        verify_bundle(target, store.app, store.clock())
        prior = json.loads(target.read_bytes())
        if (prior["surah"], prior["ayah"]) != (surah, ayah):
            raise ValueError("QF_JOB_CONTENT_CHANGED")
        return target
    verse = f"{surah}:{ayah}"
    tafsir = source.tafsir(verse)  # Missing tafsir blocks, never substitute generated text.
    verify_tafsir_reference(verse, tafsir)
    audio = directory / "recitation.mp3"
    duration = None
    try:
        store.sweep()
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
        db.execute("UPDATE clips SET state=? WHERE job=? AND state<>'FAILED'", (audio_state, job))
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
    verify_tafsir_reference(f"{value['surah']}:{value['ayah']}", tafsir)
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
