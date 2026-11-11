"""Source factory port: all downloads go through the application's source broker."""

import json
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from studio_core.network import NetworkClient as NetworkClient
from studio_core.weather import content_hash

from .model import (
    DATA,
    HADITH_LEAD,
    NEWS,
    QURAN,
    REPLAY_GAP,
    SACRED,
    TITLES,
    budget,
    canonical,
    scene,
    sha,
    timed,
    validate,
)
from .prepare_base import cut_matches as cut_matches
from .prepare_base import duration as duration
from .prepare_base import safe_xml as safe_xml
from .prepare_news import NewsPreparation
from .prepare_news import desk_order as desk_order
from .prepare_variety import VarietyPreparation

DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
# Owner decision 8 Nov 2026: one fixed theme per kind, like a TV channel's music package —
# never random. All CC BY (commercial use with credit); the credit goes into every caption.
BY3 = "CC BY 3.0 https://creativecommons.org/licenses/by/3.0/"
BY4 = "CC BY 4.0 https://creativecommons.org/licenses/by/4.0/"
FSM = "Royalty Free Music by https://www.free-stock-music.com"


def theme(artist: str, slug: str, title: str, license_: str) -> tuple[str, str]:
    page = f"https://www.free-stock-music.com/{slug}.html"
    url = f"https://www.free-stock-music.com/music/{artist}/mp3/{slug}.mp3"
    return url, chr(10).join((f"{title} | {page}", FSM, license_))


THEMES = {
    "news": theme(
        "alex-productions",
        "alex-productions-breaking-news",
        "Breaking News by Alex-Productions",
        BY3,
    ),
    "funny": theme(
        "alex-productions", "alex-productions-have-fun", "Have Fun by Alex-Productions", BY3
    ),
    "riddle": theme("fsm-team", "fsm-team-escp-pendulum", "Pendulum by FSM Team & escp", BY4),
    "tech": theme(
        "mixaund", "mixaund-tech-corporate-loop", "Tech Corporate (Loop) by Mixaund", BY4
    ),
    "style": theme(
        "fsm-team", "fsm-team-escp-downtown-walk", "Downtown Walk by FSM Team & escp", BY4
    ),
    "history": theme(
        "alexander-nakarada",
        "alexander-nakarada-tranquil-fields-eastern",
        "Tranquil Fields (Eastern) by Alexander Nakarada",
        BY4,
    ),
}


THEME_OF = {
    **dict.fromkeys(
        ("urgent", "saudi", "world", "football", "markets", "news", "news_noon"), "news"
    ),
    **dict.fromkeys(("tech", "facts", "did_you_know"), "tech"),
    "funny": "funny",
    "riddle": "riddle",
    "style": "style",
    "history": "history",
}


class Factory(NewsPreparation, VarietyPreparation):
    def build(self) -> dict[str, Any]:
        key = self.key
        day = datetime.now(ZoneInfo("Asia/Riyadh")).date()
        night_number = max(1, (day - date(2026, 9, 26)).days + 1) if key == "night" else None
        title = (
            (TITLES[key] + " • الليلة " + str(night_number).translate(DIGITS))
            if night_number
            else TITLES[key]
        )
        scenes = [scene(title, 3.2, role="hook")]
        if key in NEWS | {"tech", "style"}:
            scenes += self.news()
        elif key in {"facts", "did_you_know", "riddle", "funny"}:
            scenes += self.variety()
        elif key == "history":
            scenes += self.history(day)
            scenes[0]["source"] = self.hijri_page
        elif key in QURAN:
            # Approved local King Fahd references; no fetched or normalised Quran text.
            data = json.loads((DATA / "status.json").read_bytes())
            pool = {
                "verse": "verses",
                "friday": "friday_verses",
                "kahf": "kahf_verses",
                "fast": "fast_verses",
                "isha": "isha_verses",
            }.get(key)
            candidates = [{"surah": s, "ayahs": a} for s, a, _ in data[pool]] if pool else []
            if key == "dua":
                candidates = [
                    {"surah": s, "ayahs": a}
                    for s, a in [
                        (2, [201]),
                        (3, [8]),
                        (3, [16]),
                        (7, [23]),
                        (14, [41]),
                        (20, [25, 26, 27, 28]),
                        (25, [74]),
                        (59, [10]),
                    ]
                ]
            for item in candidates:
                try:
                    scenes += self.quran(item["surah"], item["ayahs"])
                    if not any(s.get("phrases") for s in scenes):
                        scenes += self.saadi(item["surah"], item["ayahs"], scenes)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError("LEGACY_NO_WHOLE_PASSAGE_WITHIN_BUDGET")
        elif key in {"adhkar_am", "adhkar_pm"}:
            scenes += self.adhkar("am" if key.endswith("am") else "pm")
        elif key == "hadith":
            from studio_core.presentation import faith

            scenes += self.hadiths(faith.load("hadith"), day)
        elif key == "night":
            from studio_core.presentation import faith

            scenes += self.quran(94, [5, 6])
            item = min(
                (i for i in faith.load("hadith") if i["grade"] == "صحيح"),
                key=lambda i: len(i["text"]),
            )
            spoken, length = self.hadith_voice(item["text"])
            scenes.append(
                scene(
                    item["text"],
                    max(6.0, length + 0.3, len(item["text"]) / 17),
                    audio=spoken,
                    audio_duration=length,
                    voice_kind="speech",
                    role="hadith",
                    head=HADITH_LEAD,
                    spoken_lead=len(HADITH_LEAD) + 1,
                    source=item["source"] + " • " + item["grade"],
                    hadith_id=item["id"],
                    grade=item["grade"],
                )
            )
            remaining = (
                budget(self.app, self.key)[1] - 0.3 - 5.0 - sum(s["seconds"] for s in scenes)
            )
            scenes += self.adhkar("night", minimum=8, maximum=min(24, remaining))
        elif key == "brief":
            scenes += self.brief()
        if key in NEWS | {"tech", "style"}:
            # Open with the actual first story and its image, without a title-only delay.
            scenes.pop(0)
            scenes[0]["bulletin_title"] = TITLES[key]
        credits = " • ".join(dict.fromkeys(s.get("source", "") for s in scenes if s.get("source")))
        scenes.append(
            scene(
                "تابعني للمزيد",
                4.5 if key in {"adhkar_am", "adhkar_pm"} else 5.0,
                role="cta",
                source=credits or "روّق مع خالد",
            )
        )
        total = sum(x["seconds"] for x in scenes)
        low, high, _ = budget(self.app, self.key)
        if total < low:
            # A hair over the floor: float sums landed at 60.99999 and failed the budget.
            extra = low - total + 0.01
            # Spread over every item, so no single card lingers; the brief keeps its
            # pre-timed voice track aligned by padding only its last card.
            # News has no title card, so every scene but the ending is an item.
            items = [x for x in scenes[:-1] if x.get("role") != "hook"]
            middle = scenes[-2:-1] if key == "brief" else (items or scenes[-2:-1])
            share = sum(x["seconds"] for x in middle)
            for item in middle:
                item["seconds"] += extra * item["seconds"] / share
        if total > high and key in NEWS:
            per = (high - 1.0 - 8.2) / (len(scenes) - 2)
            for item in scenes[1:-1]:
                item["seconds"] = per
        scenes, total = timed(scenes)
        for item in scenes:
            if item.get("audio"):
                self.audio.append(
                    {
                        "file": item["audio"],
                        "at": item["at"] + 0.15,
                        "seconds": item["audio_duration"],
                        "kind": item.get("voice_kind", "human"),
                    }
                )
        for item in scenes:
            if item.get("phrases"):
                # The same whole recitation again while the clip lasts; never a cut ayah.
                length = item["audio_duration"]
                room = item["seconds"] - 0.15 - length - 0.3
                item["replays"] = max(0, int(room // (length + REPLAY_GAP)))
                for k in range(1, item["replays"] + 1):
                    self.audio.append(
                        {
                            "file": item["audio"],
                            "at": round(item["at"] + 0.15 + k * (length + REPLAY_GAP), 3),
                            "seconds": length,
                            "kind": "human",
                            "repeat": True,
                        }
                    )
        if key == "brief":
            self.audio = self.brief_audio
        music = None
        if key not in SACRED:
            url, credit = THEMES[THEME_OF.get(key, "tech")]
            music = self.save(self.get(url), ".mp3", url, credit)
            duration(self.root / music)
        p = {
            "schema": 1,
            "app": self.app,
            "key": key,
            "scenes": scenes,
            "duration": total,
            "assets": self.assets,
            "audio": self.audio,
            "music": music,
            "effects": key not in SACRED,
            "sources": self.sources,
            **(
                {"night_number": night_number, "night_date": day.isoformat()}
                if night_number
                else {}
            ),
        }
        value = {
            "type": "did_you_know",
            "kind": key,
            "sacred": key in SACRED,
            "language": "ar",
            "title": TITLES[key],
            "body": "\n".join(s["body"] for s in scenes),
            "fact_id": "legacy-" + sha(canonical(p))[:32],
            "category": key,
            "source": "legacy-production",
            "source_display": "إنتاج البوت القديم",
            "source_url": "",
            "verified_at": "2026-11-03",
            "attribution": "روّق مع خالد",
            "footer": "تابعني للمزيد 👻",
            "fetched_at": datetime.now(UTC).isoformat(),
            "expires_at": time.time() + 3600,
            "legacy_production": p,
        }
        from .publication import target

        value["publication_target"] = target(value)
        value["content_hash"] = content_hash(value)
        validate(value, assets=self.root)
        (self.root / "content.json").write_bytes(canonical(value))
        return value


def prepare(app: str, key: str, job: str, root: Path, **kwargs: Any) -> dict[str, Any]:
    if (root / "content.json").is_file():
        value = json.loads((root / "content.json").read_bytes())
        p = validate(value, assets=root)
        if p["app"] != app or p["key"] != key:
            raise ValueError("LEGACY_CACHED_SCOPE_CHANGED")
        return cast(dict[str, Any], value)
    return Factory(app, key, job, root, **kwargs).build()
