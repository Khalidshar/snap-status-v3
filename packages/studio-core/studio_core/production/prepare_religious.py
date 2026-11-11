"""ReligiousPreparation methods; behavior preserved from production.prepare.Factory."""

import array
import html
import json
import math
import os
import re
import sqlite3
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from studio_core.config import Config
from studio_core.renderer import run

from .model import (
    HADITH_LEAD,
    ROOT,
    SAADI,
    SAADI_NAME,
    budget,
    phrases_for,
    saadi_text,
    scene,
    sha,
)
from .prepare_base import DIGITS, PreparationBase, cut_matches, duration, network_client


class ReligiousPreparation(PreparationBase):
    def quran(
        self, surah: int, ayahs: list[int], *, background: bool = False
    ) -> list[dict[str, Any]]:
        from studio_core.religious.quran_foundation import exact_ayah

        catalog = json.loads((ROOT / "config/production/reciters.json").read_text())
        readers = catalog["reciters"]
        if self.key in {"isha", "night"}:
            readers = [r for r in readers if r["id"] in catalog["calm"]]
        with sqlite3.connect(self.db) as db:
            last = db.execute("SELECT id FROM readers ORDER BY at DESC LIMIT 1").fetchone()
        last_name = next(
            (r["name"] for r in catalog["reciters"] if last and r["id"] == last[0]),
            last[0] if last else None,
        )
        pos = next((i for i, r in enumerate(readers) if r["name"] == last_name), -1)
        order = readers[pos + 1 :] + readers[: pos + 1]
        mp3 = [r for r in order if r["provider"] == "mp3quran"]
        fallback = [r for r in order if r["provider"] == "everyayah"]
        try:
            reads = self.json("https://mp3quran.net/api/v3/ayat_timing/reads")
        except (OSError, ValueError, RuntimeError):
            reads = []
        # Owner report 8 Nov 2026 («المكتوب آية الكرسي والصوت آية أخرى»): one file per ayah,
        # named by its number, comes first; a cut from a whole-surah file is only a fallback
        # and must match the length of the same ayah from per-ayah recordings.
        # Owner 8 Nov 2026: our Quran Foundation access (official, one file per ayah) first.
        extra = self.foundation_recitation(surah, ayahs, last_name, background=background)
        if extra:
            return extra
        references: dict[int, list[float]] = {}
        for reader in fallback + mp3:
            if reader["name"] == last_name:
                continue
            try:
                recordings = []
                # Fail locally before downloading if an official reference is absent.
                for a in ayahs:
                    exact_ayah(surah, a)
                if reader["provider"] == "mp3quran":
                    read = next(r for r in reads if str(r["id"]) == reader["id"].split(":")[1])
                    timings = self.json(
                        f"https://mp3quran.net/api/v3/ayat_timing?surah={surah}&read={read['id']}"
                    )
                    url = f"{read['folder_url'].replace('http://', 'https://').rstrip('/')}/{surah:03d}.mp3"
                    full = self.root / "surah.mp3"
                    full.write_bytes(self.get(url))
                for ayah in ayahs:
                    audio = self.root / f"ayah-{ayah}.mp3"
                    if reader["provider"] == "mp3quran":
                        row = next(r for r in timings if r["ayah"] == ayah)
                        start, end = float(row["start_time"]) / 1000, float(row["end_time"]) / 1000
                        if not math.isfinite(start + end) or start < 0 or end <= start:
                            raise ValueError("LEGACY_AYAH_TIMING_INVALID")
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
                                str(full),
                                "-ss",
                                str(max(0, start - 0.05)),
                                "-t",
                                str(end - start + 0.3),
                                "-ar",
                                "48000",
                                "-ac",
                                "2",
                                str(audio),
                            ],
                            180,
                        )
                        if ayah not in references:
                            references[ayah] = self.reference_seconds(surah, ayah, fallback)
                        if not cut_matches(duration(audio), references[ayah]):
                            raise ValueError("LEGACY_AYAH_CUT_MISMATCH")
                    else:
                        url = f"https://everyayah.com/data/{reader['id']}/{surah:03d}{ayah:03d}.mp3"
                        audio.write_bytes(self.get(url))
                    recordings.append(
                        self.recitation_scene(
                            surah,
                            ayah,
                            audio,
                            url,
                            reader["name"],
                            "mp3quran" if reader["provider"] == "mp3quran" else "EveryAyah",
                            background=background,
                        )
                    )
                if sum(x["seconds"] for x in recordings) > self.passage_room():
                    raise ValueError("LEGACY_WHOLE_PASSAGE_EXCEEDS_BUDGET")
                self.remember_reader(reader["name"])
                return recordings
            except (
                OSError,
                ValueError,
                KeyError,
                StopIteration,
                RuntimeError,
                subprocess.SubprocessError,
            ):
                continue
        raise ValueError("LEGACY_HUMAN_RECITATION_UNAVAILABLE")

    def reference_seconds(
        self, surah: int, ayah: int, readers: list[dict[str, Any]]
    ) -> list[float]:
        """Lengths of the same ayah from up to three per-ayah recordings (EveryAyah)."""
        found: list[float] = []
        for reader in readers:
            url = f"https://everyayah.com/data/{reader['id']}/{surah:03d}{ayah:03d}.mp3"
            path = self.root / f"reference-{reader['id']}-{ayah}.mp3"
            try:
                path.write_bytes(self.get(url))
                found.append(duration(path))
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                continue
            if len(found) == 3:
                break
        return found

    def saadi(
        self, surah: int, ayahs: list[int], before: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Sa'di after the verses (Muyassar stays on each verse), only if it fits whole."""
        found: list[dict[str, Any]] = []
        try:
            for ayah in ayahs:
                url = SAADI.format(surah, ayah)
                raw = self.get(url)
                self.save(raw, ".json", url, SAADI_NAME + " — مؤسسة القرآن")
                text = saadi_text(raw, surah, ayahs)
                if any(s["body"] == text for s in found):
                    continue  # one Sa'di passage can explain several of these verses
                found.append(
                    scene(
                        text,
                        (len(text) + len(SAADI_NAME)) / 17 + 1,
                        role="saadi",
                        head=SAADI_NAME,
                        saadi=[surah, ayahs],
                        tafsir_sha256=sha(text.encode()),
                        source=SAADI_NAME + " — مؤسسة القرآن",
                        source_url=url,
                    )
                )
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            return []  # Muyassar alone, still whole
        # Room left for the closing card (5 s) inside the platform's longest clip.
        room = budget(self.app, self.key)[1] - 5.0 - sum(s["seconds"] for s in before)
        if sum(s["seconds"] for s in found) > room:
            return []
        return found

    def passage_room(self) -> float:
        """Seconds a whole passage may take: the platform's longest clip minus the opening
        title (3.2 s), the closing card (5 s) and a margin. WhatsApp ~49.5, Snap verses ~111.5.
        """
        return budget(self.app, self.key)[1] - 3.2 - 5.0 - 0.3

    def hadith_voice(self, text: str, identifier: str) -> tuple[str, float]:
        from studio_core.religious.hadith_voice import read

        raw = read(identifier, text)
        path = self.save(
            raw, ".mp3", "asset://hadith/" + identifier + ".mp3", "صوت حديث مخزن ومعتمد"
        )
        return path, duration(self.root / path)

    def daily_hadith(self, day: date) -> dict[str, Any]:
        from studio_core.religious.hadith_daily import choose

        return choose(day, self.db, self.app, self.job)

    def hadiths(self, day: date) -> list[dict[str, Any]]:
        """One approved hadith for the date and season, without repeating the cycle."""
        found: list[dict[str, Any]] = []
        for item in [self.daily_hadith(day)]:
            spoken, length = self.hadith_voice(item["text"], item["id"])
            found.append(
                scene(
                    item["text"],
                    max(length + 1.0, len(item["text"]) / 17 + 1),
                    role="hadith",
                    head=HADITH_LEAD,
                    spoken_lead=0,
                    source=item["source"] + " • " + item["grade"],
                    hadith_id=item["id"],
                    grade=item["grade"],
                    audio=spoken,
                    audio_duration=length,
                    voice_kind="speech",
                )
            )
        return found

    def remember_reader(self, name: str) -> None:
        with sqlite3.connect(self.db, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT job,id FROM readers ORDER BY at DESC LIMIT 1").fetchone()
            if current and current[0] != self.job and current[1] == name:
                raise ValueError("LEGACY_READER_REPEATED")
            db.execute(
                "INSERT OR REPLACE INTO readers VALUES(?,?,?)", (self.job, name, time.time())
            )

    def recitation_scene(
        self,
        surah: int,
        ayah: int,
        audio: Path,
        url: str,
        name: str,
        provider: str,
        *,
        background: bool,
        segments: list[list[int]] | None = None,
    ) -> dict[str, Any]:
        from studio_core.religious.quran_foundation import exact_ayah

        seconds = duration(audio)
        run(
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-nostdin",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(audio),
                "-f",
                "null",
                "-",
            ],
            180,
        )
        result = self.json(
            f"https://quranenc.com/api/v1/translation/aya/arabic_moyassar/{surah}/{ayah}"
        )["result"]
        if int(result["sura"]) != surah or int(result["aya"]) != ayah:
            raise ValueError("LEGACY_TAFSIR_REFERENCE_CHANGED")
        tafsir = html.unescape(re.sub("<[^>]*>", "", result["translation"]))
        text = exact_ayah(surah, ayah)
        hold = seconds + 0.8 if background else seconds + 0.8 + max(len(tafsir) / 17 + 1, 5.0)
        extra: dict[str, Any] = {}
        if segments is not None:
            # «تلاوة» (owner 8 Nov 2026): phrases follow the reciter; the tafsir is not paged.
            try:
                extra["phrases"] = phrases_for(text, segments, seconds)
                hold = seconds + 0.8
            except ValueError:
                pass  # word counts differ: the whole ayah is shown, never a guessed phrase
        return scene(
            text,
            hold,
            role="verse",
            verse=[surah, ayah],
            tafsir=tafsir,
            tafsir_sha256=sha(tafsir.encode()),
            source=f"بصوت {name} — التلاوة: {provider}",
            audio=self.local(audio, url, name),
            audio_duration=seconds,
            **extra,
        )

    def foundation_recitation(
        self, surah: int, ayahs: list[int], last_name: str | None, *, background: bool
    ) -> list[dict[str, Any]]:
        # Keys stay exclusively in the already configured QF broker. Missing service
        # never blocks the two keyless providers above, and never triggers TTS.
        if os.environ.get("MADAR_QURAN_AUDIO") != "1":
            return []
        from studio_core.religious.quran_commands import QuranSocketPolicy
        from studio_core.religious.quran_foundation import CATALOG, Source
        from studio_core.religious.quran_pipeline import store_for

        short = "snap" if self.app == "snap-bot-v2" else "status"
        client = network_client(
            cast(Config, QuranSocketPolicy(Path(f"/run/madar/{short}-quran.sock")))
        )
        try:
            ready = json.loads(client.get("quran://ready").body)
            if ready.get("configured") is not True:
                return []
            source = Source(client, ready["environment"])
            choices = source.catalog(json.loads(CATALOG.read_bytes()))["reciters"]
            store = store_for(self.app)
            store.sweep()
            for reader in choices:
                if reader["name"] == last_name:
                    continue
                qf_job = sha((self.job + ":qf:" + str(reader["id"])).encode())[:32]
                _, expires = store.reserve(qf_job, {"reciters": [reader]})
                try:
                    scenes = []
                    for ayah in ayahs:
                        store.sweep()
                        audio = store.directory(qf_job) / f"{surah}-{ayah}.mp3"
                        audio.write_bytes(source.audio(f"{surah}:{ayah}", reader))
                        try:
                            timing = source.segments(f"{surah}:{ayah}", reader)
                        except ValueError:
                            timing = None  # no estimated timing: the classic page instead
                        item = self.recitation_scene(
                            surah,
                            ayah,
                            audio,
                            "quran-foundation:" + str(reader["id"]) + f"/{surah}/{ayah}",
                            reader["name"],
                            "مؤسسة القرآن",
                            background=background,
                            segments=timing,
                        )
                        asset = next(a for a in self.assets if a["file"] == item["audio"])
                        asset.update({"qf_job": qf_job, "expires_at": expires})
                        store.track(self.root / item["audio"], qf_job, expires, asset["sha256"])
                        scenes.append(item)
                    if sum(s["seconds"] for s in scenes) > self.passage_room():
                        raise ValueError("LEGACY_WHOLE_PASSAGE_EXCEEDS_BUDGET")
                    self.remember_reader(reader["name"])
                    return scenes
                except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
                    store.failed(qf_job)
                    # 8 Nov 2026: a rejected attempt (e.g. an ayah missing from the approved
                    # text) left its audio in the plan bound to a failed reservation, and the
                    # isolated Snap renderer refused the whole clip when tracking it.
                    self.assets = [a for a in self.assets if a.get("qf_job") != qf_job]
                    continue
        except (OSError, ValueError, KeyError, RuntimeError):
            return []
        return []

    @staticmethod
    def audio_levels(path: Path) -> list[float]:
        raw = subprocess.run(  # noqa: S603 — fixed local-only decoder argv
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(path),
                "-ar",
                "6000",
                "-ac",
                "1",
                "-f",
                "s16le",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=120,
        ).stdout
        samples = array.array("h", raw)
        return [
            min(
                1.0,
                math.sqrt(sum(v * v for v in samples[i : i + 400]) / len(samples[i : i + 400]))
                / 8000,
            )
            for i in range(0, len(samples), 400)
        ]

    def adhkar(
        self, period: str, *, minimum: float = 44, maximum: float = 50
    ) -> list[dict[str, Any]]:
        """One official human recording per exact dhikr; no guessed archive timings."""
        chapter = 28 if period == "night" else 27
        url = f"https://www.hisnmuslim.com/api/ar/{chapter}.json"
        response = self.json(url)
        rows = list(next(iter(response.values())))
        if period not in {"am", "pm"}:
            return self.adhkar_from(rows, chapter, minimum, maximum)
        # Chapter 27 holds morning and evening together (owner report 8 Nov 2026: evening was
        # a copy of morning): morning reads it forward, evening backward, and the start moves
        # daily; the plain order is the fallback when a rotated one cannot fill the clip.
        base = rows if period == "am" else rows[::-1]
        shift = datetime.now(ZoneInfo("Asia/Riyadh")).date().toordinal() % max(1, len(base))
        for order in (base[shift:] + base[:shift], base):
            try:
                return self.adhkar_from(order, chapter, minimum, maximum)
            except ValueError:
                continue
        raise ValueError("LEGACY_MATCHED_HUMAN_ADHKAR_UNAVAILABLE")

    def adhkar_from(
        self, rows: list[dict[str, Any]], chapter: int, minimum: float, maximum: float
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        total = 0.0
        for row in rows:
            text = row["ARABIC_TEXT"]
            # Quran is admitted exclusively from King Fahd, never this API. Skip
            # records with variants/instructions: their audio is not a single text.
            if any(token in text for token in ("\ufd3f", "[", "إذا", "أصبح", "أمسى", "أمسيت")):
                continue
            audio_url = row["AUDIO"].replace("http://", "https://", 1)
            if not re.fullmatch(r"https://www\.hisnmuslim\.com/audio/ar/[0-9]+\.mp3", audio_url):
                raise ValueError("LEGACY_DHIKR_AUDIO_SOURCE_INVALID")
            try:
                audio = self.save(self.get(audio_url), ".mp3", audio_url, "حصن المسلم — تسجيل بشري")
                seconds = duration(self.root / audio)
                if total + seconds + 0.3 > maximum:
                    continue
                selected.append(
                    scene(
                        text,
                        seconds + 0.3,
                        role="adhkar",
                        source="حصن المسلم",
                        number="التكرار: " + str(row["REPEAT"]).translate(DIGITS),
                        dhikr_id=row["ID"],
                        dhikr_chapter=chapter,
                        levels=self.audio_levels(self.root / audio),
                        audio=audio,
                        audio_duration=seconds,
                    )
                )
                total += seconds + 0.3
                if total >= minimum:
                    return selected
                if len(selected) >= 16:
                    break
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                continue
        raise ValueError("LEGACY_MATCHED_HUMAN_ADHKAR_UNAVAILABLE")
