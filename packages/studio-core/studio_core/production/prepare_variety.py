"""VarietyPreparation methods; behavior preserved from production.prepare.Factory."""

import json
import re
import subprocess
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from . import parsing
from .model import (
    DATA,
    canonical,
    scene,
    sha,
)
from .prepare_base import DIGITS
from .prepare_religious import ReligiousPreparation


class VarietyPreparation(ReligiousPreparation):
    def history(self, day: date) -> list[dict[str, Any]]:
        """حدث اليوم: Islamic-history events on today's Hijri date (owner decision 3 Nov).

        Source: the Arabic Wikipedia page of the Hijri day; Saudi and early Islamic first,
        nothing from the last 30 Hijri years, no killing or massacres.
        """
        hijri = self.json(f"https://api.aladhan.com/v1/gToH/{day:%d-%m-%Y}")["data"]["hijri"]
        month = re.sub(r"[ً-ْ]", "", hijri["month"]["ar"])
        page = f"{int(hijri['day'])} {month}"
        self.hijri_page = page
        wiki = self.json(
            "https://ar.wikipedia.org/w/api.php?"
            + urlencode(
                {
                    "action": "parse",
                    "page": page,
                    "prop": "wikitext",
                    "format": "json",
                    "redirects": 1,
                }
            )
        )["parse"]["wikitext"]["*"]
        avoid = ("اغتيال", "مجزرة", "مذبحة", "إعدام", "انتحار", "تفجير", "قتل", "مقتل", "اغتصاب")
        saudi = ("السعودية", "عبد العزيز", "الحجاز", "نجد", "مكة", "المدينة المنورة", "آل سعود")
        latest = int(hijri["year"]) - 30
        events = []
        unlink = r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]"
        # Only the «أحداث» section: births and deaths are not events of the day.
        section = re.split(r"^==\s*أحداث\s*==\s*$", wiki, maxsplit=1, flags=re.M)
        happened = (
            re.split(r"^==[^=].*$", section[1], maxsplit=1, flags=re.M)[0]
            if len(section) > 1
            else ""
        )
        for line in happened.splitlines():
            # The year itself is usually a link: "* [[512 هـ|512هـ]] - ..." or "* [[359 هـ]] - ...".
            found = re.match(
                r"\*\s*(?:\[\[(?:[^\]|]*\|)?)?\s*(\d{1,4})\s*هـ\s*(?:\]\])?\s*[^\w\s]\s*(.+)", line
            )
            if not found or int(found[1]) > latest:
                continue
            raw = re.sub(r"<ref[^>]*/>|<ref.*?</ref>|\{\{.*?\}\}", "", found[2])
            link_match = re.search(r"\[\[([^|\]#]+)", raw)
            text = parsing.clean(re.sub(unlink, r"\1", raw), 220)
            if len(text) < 20 or any(word in text for word in avoid):
                continue
            rank = (0 if any(word in text for word in saudi) else 1, int(found[1]))
            events.append((rank, int(found[1]), text, link_match[1] if link_match else ""))
        events.sort()
        need = 4 if self.app == "snap-bot-v2" else 3
        if len(events) < 2:
            raise ValueError("LEGACY_HISTORY_UNAVAILABLE")
        scenes = []
        pictures: set[str] = set()  # one event's picture is never reused for another event
        for n, (_, year, text, link) in enumerate(events[:need], 1):
            extra: dict[str, Any] = {}
            if link:
                try:
                    summary = self.json(
                        "https://ar.wikipedia.org/api/rest_v1/page/summary/" + quote(link)
                    )
                    thumb = summary.get("originalimage") or summary.get("thumbnail") or {}
                    if thumb.get("source") and thumb["source"] not in pictures:
                        pictures.add(thumb["source"])
                        extra = {
                            "image": self.picture(thumb["source"], thumb["source"], "ويكيبيديا"),
                            "image_credit": "ويكيبيديا",
                        }
                except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
                    extra = {}
            scenes.append(
                scene(
                    text,
                    max(9.0, len(text) / 15 + 3),
                    role="history",
                    head="في مثل هذا اليوم • سنة " + str(year).translate(DIGITS) + "هـ",
                    number=f"{n}/{min(need, len(events))}".translate(DIGITS),
                    source="ويكيبيديا العربية • " + page,
                    **extra,
                )
            )
        return scenes

    def variety(self) -> list[dict[str, Any]]:
        key = "facts" if self.key == "did_you_know" else self.key
        items = json.loads(
            (DATA / ("riddles.json" if key == "riddle" else f"{key}.json")).read_text()
        )
        # Snap runs 65 s+: more items instead of one item left on screen for half a minute.
        need = 3 if key == "riddle" else (6 if self.app == "snap-bot-v2" else 4)
        selected = []
        for item in items:
            identity = sha(canonical(item))
            if self.used(identity, reserve=False):
                continue
            selected.append(item)
            self.used(identity)
            if len(selected) == need:
                break
        if len(selected) != need:
            raise ValueError("LEGACY_CATALOG_EXHAUSTED")
        scenes = []
        for n, item in enumerate(selected, 1):
            number = str(n).translate(DIGITS)
            if key == "riddle":
                spoken, length = self.spoken(item["a"], "riddle")
                scenes += [
                    scene(
                        item["q"],
                        8.2,
                        role="question",
                        number="لغز " + number,
                        countdown=5,
                        countdown_after=3.0,
                    ),
                    scene(
                        item["a"],
                        max(3.6, length + 0.3),
                        role="answer",
                        head=item["q"],
                        audio=spoken,
                        audio_duration=length,
                        voice_kind="speech",
                    ),
                ]
            elif key == "funny":
                scenes.append(
                    scene(
                        item["a"],
                        8.5,
                        role="funny",
                        punch=item["b"],
                        reveal_after=3.5,
                        number=number,
                    )
                )
            else:
                scenes.append(scene(str(item), 8.0, role="fact", number=number))
        return scenes

    def brief(self) -> list[dict[str, Any]]:
        data = json.loads((DATA / "status.json").read_text())
        day = datetime.now(ZoneInfo("Asia/Riyadh"))
        info = self.json(
            f"https://api.aladhan.com/v1/timingsByCity/{day:%d-%m-%Y}?city=Riyadh&country=SA&method=4"
        )["data"]
        labels = {
            "Fajr": "الفجر",
            "Dhuhr": "الظهر",
            "Asr": "العصر",
            "Maghrib": "المغرب",
            "Isha": "العشاء",
        }

        def clock(value: str) -> str:
            h, m = map(int, value[:5].split(":"))
            return f"{h % 12 or 12}:{m:02d} {'ص' if h < 12 else 'م'}"

        prayers = "\n".join(
            f"{v} {clock(info['timings'][k])}" for k, v in labels.items()
        ).translate(DIGITS)
        events = []
        for name, m, d in [("اليوم الوطني", 9, 23), ("يوم التأسيس", 2, 22)]:
            at = day.date().replace(month=m, day=d)
            if at <= day.date():
                at = at.replace(year=at.year + 1)
            events.append((at, name))
        hy = int(info["date"]["hijri"]["year"])
        for name, hd, hm in [("رمضان", 1, 9), ("عيد الفطر", 1, 10), ("عيد الأضحى", 10, 12)]:
            for year in (hy, hy + 1):
                stamp = self.json(f"https://api.aladhan.com/v1/hToG/{hd:02d}-{hm:02d}-{year}")[
                    "data"
                ]["gregorian"]["date"]
                at = datetime.strptime(stamp, "%d-%m-%Y").date()
                if at > day.date():
                    events.append((at, name))
                    break
        at, event = min(events)
        weather = self.json(
            "https://api.open-meteo.com/v1/forecast?latitude=24.71&longitude=46.68&timezone=Asia%2FRiyadh&forecast_days=1&daily=temperature_2m_max,temperature_2m_min"
        )
        other = self.json(
            "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=24.71&lon=46.68"
        )
        temperatures = [
            r["data"]["instant"]["details"]["air_temperature"]
            for r in other["properties"]["timeseries"]
            if datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
            .astimezone(ZoneInfo("Asia/Riyadh"))
            .date()
            == day.date()
        ]
        hi, lo = (
            weather["daily"]["temperature_2m_max"][0],
            weather["daily"]["temperature_2m_min"][0],
        )
        # Later in the day the hourly feed has lost the cold early hours: compare the low
        # only while most of the day is still there.
        full = len(temperatures) >= 18
        if (
            not temperatures
            or abs(hi - max(temperatures)) > 3
            or (full and abs(lo - min(temperatures)) > 3)
        ):
            raise ValueError("LEGACY_BRIEF_WEATHER_DISAGREES")
        fact = data["facts"][day.toordinal() % len(data["facts"])]
        hist, year = data["history"][day.toordinal() % len(data["history"])]
        # Old bot: calm human recitation (~40 s) under all four cards; one short passage left
        # most of the minute silent (owner sample review, 8 Nov 2026). Whole approved passages
        # follow one another until the cards are covered.
        self.brief_audio = []
        cursor = 3.2
        for surah, ayahs in ((94, [5, 6, 7, 8]), (89, [27, 28, 29, 30]), (112, [1, 2, 3, 4])):
            if cursor >= 3.2 + 35.0:
                break
            try:
                verses = self.quran(surah, ayahs, background=True)
            except ValueError:
                continue
            if cursor + sum(v["audio_duration"] for v in verses) > 3.2 + 45.0:
                continue  # a passage is never cut: it must end with the four cards
            for v in verses:
                self.brief_audio.append(
                    {"file": v["audio"], "at": cursor, "seconds": v["audio_duration"]}
                )
                cursor += v["audio_duration"]
        if not self.brief_audio:
            raise ValueError("LEGACY_BRIEF_RECITATION_UNAVAILABLE")
        return [
            scene(prayers, 12.0, role="brief", head="أوقات الصلاة • الرياض"),
            scene(
                f"باقي {(at - day.date()).days} يوم على {event}".translate(DIGITS),
                12.0,
                role="brief",
                head="العدّاد",
            ),
            scene(fact, 12.0, role="brief", head="هل تعلم؟"),
            scene(
                hist,
                9.0,
                role="brief",
                head=f"من تاريخ الإسلام • {year} هـ",
                source=f"الرياض: العظمى {hi} والصغرى {lo} — متحقق من مصدرين",
            ),
        ]
