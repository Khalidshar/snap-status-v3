"""Immutable scene contract for the old mechanism inside the approved frame."""

import hashlib
import html
import json
import math
import re
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from studio_core.weather import content_hash

ROOT = Path(__file__).resolve().parents[4]
DATA = Path(__file__).with_name("data")


def budget(app: str, key: str = "") -> tuple[float, float, int]:
    """(shortest, longest seconds, largest bytes) per platform (owner decision 3 Nov).

    Snap Spotlight pays only for clips longer than one minute; a WhatsApp Status is 58 s at most.
    Snap verse clips may run 120 s so the whole Sa'di tafsir fits (owner decision 6 Nov);
    48 MB stays under the Telegram bot upload limit.
    """
    if app == "snap-bot-v2" and key in QURAN:
        return 61.0, 120.0, 48_000_000
    if app == "snap-bot-v2":
        return 61.0, 90.0, 40_000_000
    return 32.0, 58.0, 16_000_000


NEWS = {"urgent", "saudi", "world", "football", "markets", "news", "news_noon"}
QURAN = {"verse", "dua", "friday", "kahf", "fast", "isha"}
SACRED = QURAN | {"adhkar_am", "adhkar_pm", "hadith", "night", "brief"}
# Owner 8 Nov 2026: «لابد من ذكر قال رسول الله» — shown and said before every hadith.
HADITH_LEAD = "قال رسول الله صلى الله عليه وسلم:"
TITLES = {
    "urgent": "عاجل • النزاعات",
    "saudi": "أخبار السعودية",
    "world": "أخبار العالم",
    "football": "ملخص كرة القدم",
    "markets": "الأسواق والاقتصاد",
    "news": "وش صار اليوم؟",
    "news_noon": "أخبار الظهر",
    "tech": "التقنية",
    "style": "الأناقة والجمال",
    "verse": "آية اليوم",
    "dua": "دعاء اليوم",
    "friday": "رسالة الجمعة",
    "kahf": "سورة الكهف",
    "fast": "تذكير الصيام",
    "isha": "ليلة طيبة",
    "adhkar_am": "أذكار الصباح",
    "adhkar_pm": "أذكار المساء",
    "hadith": "حديث اليوم",
    "night": "قبل ما تنام",
    "brief": "يومك في دقيقة",
    "facts": "هل تعلم؟",
    "did_you_know": "هل تعلم؟",
    "riddle": "لغز اليوم",
    "funny": "مواقف طريفة",
    "history": "حدث اليوم",
}


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def resource(root: Path, name: str, expected: str) -> Path:
    if not re.fullmatch(r"assets/[a-zA-Z0-9_.-]+", name) or not re.fullmatch(
        "[a-f0-9]{64}", expected
    ):
        raise ValueError("LEGACY_ASSET_NAME_INVALID")
    path = root / name
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError("LEGACY_ASSET_UNSAFE")
    if sha(path.read_bytes()) != expected:
        raise ValueError("LEGACY_ASSET_CHANGED")
    return path


def original_field(item: dict[str, Any], field: str, root: Path, assets: dict[str, Any]) -> str:
    """Bind displayed Arabic to the retained translation of its exact source field."""
    from urllib.parse import parse_qs, urlsplit

    from .translation import decode, request_url

    target = str(item.get(field, ""))
    name = item.get("translations", {}).get(field)
    if name is None:
        return target
    proof = assets.get(name)
    if proof is None:
        raise ValueError("TRANSLATION_PROOF_MISSING")
    query = parse_qs(urlsplit(proof["source_url"]).query)
    originals = query.get("q", [])
    if len(originals) != 1 or proof["source_url"] != request_url(originals[0]):
        raise ValueError("TRANSLATION_PROOF_SCOPE_CHANGED")
    raw = resource(root, name, proof["sha256"]).read_bytes()
    if target != decode(originals[0], raw):
        raise ValueError("TRANSLATION_DISPLAY_CHANGED")
    return str(originals[0])


def validate(value: dict[str, Any], *, assets: Path | None = None) -> dict[str, Any]:
    p = value.get("legacy_production")
    if not isinstance(p, dict) or p.get("schema") != 1 or p.get("key") not in TITLES:
        raise ValueError("LEGACY_CONTRACT_INVALID")
    if p.get("app") not in {"snap-bot-v2", "status-bot-v2"} or value.get(
        "content_hash"
    ) != content_hash(value):
        raise ValueError("LEGACY_SCOPE_OR_HASH_CHANGED")
    scenes = p.get("scenes")
    if not isinstance(scenes, list) or not 3 <= len(scenes) <= 18:
        raise ValueError("LEGACY_SCENES_INVALID")
    if (
        value.get("body") != "\n".join(s["body"] for s in scenes)
        or value.get("fact_id") != "legacy-" + sha(canonical(p))[:32]
    ):
        raise ValueError("LEGACY_CONTENT_BINDING_CHANGED")
    asset_map = {a["file"]: a for a in p.get("assets", [])}
    if len(asset_map) != len(p.get("assets", [])):
        raise ValueError("LEGACY_DUPLICATE_ASSET")
    for name in [
        p.get("music"),
        *[s.get("image") for s in scenes],
        *[a.get("file") for a in p.get("audio", [])],
    ]:
        if name is not None and name not in asset_map:
            raise ValueError("LEGACY_UNDECLARED_ASSET")
    for sound in p.get("audio", []):
        if (
            not all(
                isinstance(sound.get(k), (int, float)) and math.isfinite(sound[k])
                for k in ("at", "seconds")
            )
            or sound["at"] < 0
            or sound["seconds"] <= 0
            or sound["at"] + sound["seconds"] > p["duration"]
        ):
            raise ValueError("LEGACY_AUDIO_TRUNCATED")
    at = 0.0
    for scene in scenes:
        seconds = scene.get("seconds")
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("LEGACY_TIMING_INVALID")
        if scene.get("at") != at:
            raise ValueError("LEGACY_SEQUENCE_CHANGED")
        at += seconds
        if not isinstance(scene.get("body"), str) or not scene["body"]:
            raise ValueError("LEGACY_EMPTY_SCENE")
        if scene.get("verse"):
            from studio_core.religious.quran_foundation import exact_ayah

            s, a = scene["verse"]
            if scene["body"] != exact_ayah(s, a):
                raise ValueError("LEGACY_SACRED_TEXT_CHANGED")
            if scene.get("phrases"):
                # «تلاوة»: the tafsir goes to the caption, the recitation is never cut.
                check_phrases(scene)
                if scene["seconds"] < scene.get("audio_duration", 0) + 0.15:
                    raise ValueError("LEGACY_VERSE_OR_TAFSIR_TRUNCATED")
            elif scene["seconds"] < max(
                len(scene.get("tafsir", "")) / 17 + 1, scene.get("audio_duration", 0) + 0.15
            ):
                raise ValueError("LEGACY_VERSE_OR_TAFSIR_TRUNCATED")
            if scene.get("tafsir_sha256") != sha(scene.get("tafsir", "").encode()):
                raise ValueError("LEGACY_TAFSIR_CHANGED")
        if scene.get("image") and not scene.get("image_credit"):
            raise ValueError("LEGACY_IMAGE_CREDIT_MISSING")
    low, high, _ = budget(p.get("app", ""), p.get("key", ""))
    if not low <= at <= high or abs(p.get("duration", 0) - at) > 0.001:
        raise ValueError("LEGACY_DURATION_OUT_OF_BUDGET")
    if scenes[-1].get("role") != "cta":
        raise ValueError("LEGACY_FOLLOW_CARD_MISSING")
    if p["key"] in NEWS:
        stories = [s for s in scenes if s.get("role") == "story"]
        if not 2 <= len(stories) <= 4 or (p["key"] == "urgent" and len(stories) != 3):
            raise ValueError("LEGACY_BULLETIN_INCOMPLETE")
        if any(
            not s.get("image") or not s.get("source") or not s.get("published_label")
            for s in stories
        ):
            raise ValueError("LEGACY_BULLETIN_SOURCE_MISSING")
    if p["key"] == "night":
        night_date = date.fromisoformat(p.get("night_date", ""))
        number = max(1, (night_date - date(2026, 9, 26)).days + 1)
        if (
            p.get("night_number") != number
            or "الليلة " + str(number).translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
            not in scenes[0]["body"]
        ):
            raise ValueError("LEGACY_NIGHT_NUMBER_CHANGED")
    for item in scenes:
        if item.get("role") == "hadith":
            from studio_core.presentation import faith

            hadith = next(
                (i for i in faith.load("hadith") if i["id"] == item.get("hadith_id")), None
            )
            if (
                hadith is None
                or hadith["grade"] != "صحيح"
                or item.get("grade") != hadith["grade"]
                or item["body"] != hadith["text"]
                or item.get("source") != hadith["source"] + " • " + hadith["grade"]
            ):
                raise ValueError("LEGACY_HADITH_CHANGED")
    sacred = p["key"] in SACRED
    if value.get("sacred") is not sacred:
        raise ValueError("LEGACY_SACRED_POLICY_CHANGED")
    if sacred and (p.get("music") or p.get("effects")):
        raise ValueError("LEGACY_RELIGIOUS_TONE_FORBIDDEN")
    if p["key"] in QURAN | {"adhkar_am", "adhkar_pm", "night", "brief"} and not p.get("audio"):
        raise ValueError("LEGACY_HUMAN_AUDIO_REQUIRED")
    if p["key"] in {"facts", "did_you_know", "funny", "riddle"}:
        catalog = json.loads(
            (
                DATA
                / (
                    {"riddle": "riddles.json", "did_you_know": "facts.json"}.get(
                        p["key"], p["key"] + ".json"
                    )
                )
            ).read_bytes()
        )
        core = scenes[1:-1]
        if p["key"] in {"facts", "did_you_know"} and (
            len(core) != (6 if p.get("app") == "snap-bot-v2" else 4)
            or any(s["body"] not in catalog for s in core)
        ):
            raise ValueError("LEGACY_FACT_CATALOG_CHANGED")
        if p["key"] == "funny" and (
            len(core) != (6 if p.get("app") == "snap-bot-v2" else 4)
            or any({"a": s["body"], "b": s.get("punch")} not in catalog for s in core)
        ):
            raise ValueError("LEGACY_FUNNY_CATALOG_CHANGED")
        if p["key"] == "riddle" and (
            len(core) != 6
            or any(
                {"q": core[i]["body"], "a": core[i + 1]["body"]} not in catalog
                or core[i].get("countdown") != 5
                for i in (0, 2, 4)
            )
        ):
            raise ValueError("LEGACY_RIDDLE_CATALOG_CHANGED")
    for asset in p.get("assets", []):
        if (
            asset.get("qf_job")
            and not time.time() < asset.get("expires_at", 0) <= time.time() + 7 * 86400
        ):
            raise ValueError("LEGACY_QF_ASSET_EXPIRED")
        if assets is not None:
            resource(assets, asset["file"], asset["sha256"])
    if assets is not None:
        for item in scenes:
            if item.get("role") == "adhkar":
                chapter = item.get("dhikr_chapter")
                url = f"https://www.hisnmuslim.com/api/ar/{chapter}.json"
                responses = [a for a in p["assets"] if a["source_url"] == url]
                if chapter not in {27, 28} or not responses:
                    raise ValueError("LEGACY_DHIKR_SOURCE_MISSING")
                source_asset = responses[-1]
                data = json.loads(
                    resource(assets, source_asset["file"], source_asset["sha256"]).read_bytes(),
                    strict=False,
                )
                row = next(
                    (r for r in next(iter(data.values())) if r["ID"] == item.get("dhikr_id")), None
                )
                recording = asset_map.get(item.get("audio"))
                if (
                    row is None
                    or item["body"] != row["ARABIC_TEXT"]
                    or item.get("number")
                    != "التكرار: "
                    + str(row["REPEAT"]).translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
                    or recording is None
                    or recording["source_url"] != row["AUDIO"].replace("http://", "https://", 1)
                ):
                    raise ValueError("LEGACY_DHIKR_TEXT_AUDIO_CHANGED")
            if item.get("verse"):
                surah, ayah = item["verse"]
                url = f"https://quranenc.com/api/v1/translation/aya/arabic_moyassar/{surah}/{ayah}"
                responses = [a for a in p["assets"] if a["source_url"] == url]
                if not responses:
                    raise ValueError("LEGACY_TAFSIR_SOURCE_MISSING")
                raw = resource(assets, responses[-1]["file"], responses[-1]["sha256"]).read_bytes()
                data = json.loads(raw)["result"]
                expected = html.unescape(re.sub("<[^>]*>", "", data["translation"]))
                if (
                    expected != item["tafsir"]
                    or int(data["sura"]) != surah
                    or int(data["aya"]) != ayah
                ):
                    raise ValueError("LEGACY_TAFSIR_SOURCE_CHANGED")
            if item.get("role") == "saadi":
                surah, ayahs = item["saadi"]
                url = item.get("source_url")
                responses = [a for a in p["assets"] if a["source_url"] == url]
                if p["key"] not in QURAN or url not in {SAADI.format(surah, a) for a in ayahs}:
                    raise ValueError("LEGACY_SAADI_SOURCE_MISSING")
                if not responses:
                    raise ValueError("LEGACY_SAADI_SOURCE_MISSING")
                raw = resource(assets, responses[-1]["file"], responses[-1]["sha256"]).read_bytes()
                if saadi_text(raw, surah, ayahs) != item["body"] or item.get(
                    "tafsir_sha256"
                ) != sha(item["body"].encode()):
                    raise ValueError("LEGACY_SAADI_SOURCE_CHANGED")
            if item.get("role") in {"story", "headline", "point"}:
                from studio_core.sources.news import MAX_AGE, news_sources

                key = "news" if p["key"] == "news_noon" else p["key"]
                allowed = {s.id: s for s in news_sources(p["key"], production=True)}
                source = allowed.get(item.get("source_id"))
                if (
                    source is None
                    or source.name != item.get("source")
                    or not 0 <= time.time() - item.get("published", 0) < MAX_AGE[key]
                ):
                    raise ValueError("LEGACY_NEWS_SOURCE_OR_TIME_INVALID")
                article = asset_map.get(item.get("article"))
                if article is None or article["source_url"] != item.get("source_url"):
                    raise ValueError("LEGACY_ARTICLE_PROOF_MISSING")
                from . import parsing
                from .prepare import safe_xml

                page = resource(assets, article["file"], article["sha256"]).read_text()
                text = parsing.words_only(parsing.body_text(page))
                feed = asset_map.get(item.get("feed"))
                if feed is None or feed["source_url"] != source.url:
                    raise ValueError("LEGACY_FEED_PROOF_MISSING")
                raw = resource(assets, feed["file"], feed["sha256"]).read_bytes()
                if source.kind == "rss":
                    candidates = [
                        e
                        for e in safe_xml(raw).iter("item")
                        if (e.findtext("link") or "").strip() == item["source_url"]
                    ]
                    if not candidates:
                        raise ValueError("LEGACY_STORY_NOT_IN_FEED")
                    entry = candidates[0]
                    title = parsing.clean(entry.findtext("title") or "")
                    detail = parsing.clean(
                        entry.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
                        or entry.findtext("description")
                        or ""
                    )
                    if (
                        item["role"] in {"story", "headline"}
                        and original_field(
                            item, "head" if "head" in item else "body", assets, asset_map
                        )
                        != title
                    ):
                        raise ValueError("LEGACY_HEADLINE_CHANGED")
                else:
                    detail = text
                if item["role"] == "story" and item["body"].rstrip(
                    "…"
                ) not in detail + " " + parsing.clean(text):
                    raise ValueError("LEGACY_DETAIL_NOT_IN_SOURCE")
                if item["role"] == "point":
                    pairs = parsing.plain_points(parsing.body_text(page), detail)
                    if not any(
                        p["body"] == original_field(item, "body", assets, asset_map)
                        and p["head"] == original_field(item, "head", assets, asset_map)
                        for p in pairs
                    ):
                        raise ValueError("LEGACY_POINT_NOT_IN_ARTICLE")
                if urlsplit(item["source_url"]).hostname == "news.google.com":
                    raise ValueError("LEGACY_AGGREGATOR_IS_NOT_ARTICLE")
    return p


# Owner decision (6 Nov 2026): the full Muyassar always, and the full Sa'di beside it
# whenever the clip's time allows. Never shortened; markup removed, words untouched.
SAADI = "https://api.quran.com/api/v4/tafsirs/91/by_ayah/{}:{}"
SAADI_NAME = "تفسير السعدي"


def saadi_text(raw: bytes, surah: int, ayahs: list[int]) -> str:
    from studio_core.religious.quran_foundation import literal_tafsir

    data = json.loads(raw)["tafsir"]
    covered = {tuple(int(x) for x in k.split(":")) for k in data.get("verses") or {}}
    if data.get("resource_id") != 91 or not covered or not covered <= {(surah, a) for a in ayahs}:
        raise ValueError("LEGACY_SAADI_SCOPE")
    return literal_tafsir(data["text"]).strip()


def scene(body: str, seconds: float, **extra: Any) -> dict[str, Any]:
    return {"body": body, "seconds": seconds, **extra}


def timed(scenes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float]:
    at = 0.0
    for item in scenes:
        item["at"] = at
        at += item["seconds"]
    return scenes, at


# Owner decision 8 Nov 2026 («تلاوة»): a short phrase of the official text follows the reciter.
PAUSE_MARKS = "ۖۗۘۙۚۛۜ"
PHRASE_WORDS = 3
REPLAY_GAP = 0.6


def ayah_words(text: str) -> list[str]:
    """The official ayah split on its own spaces; the last token (۝ and number) is not a word."""
    tokens = text.split(" ")
    return tokens[:-1] if tokens and tokens[-1].startswith("۝") else tokens


def phrases_for(text: str, segments: list[list[int]], seconds: float) -> list[dict[str, Any]]:
    """Groups of up to three words, closed early at a pause mark, timed by the reciter's words."""
    words = ayah_words(text)
    if len(segments) != len(words):
        raise ValueError("LEGACY_WORD_TIMING_MISMATCH")
    groups: list[list[int]] = []
    group: list[int] = []
    for i, word in enumerate(words):
        group.append(i)
        if any(c in word for c in PAUSE_MARKS) or len(group) == PHRASE_WORDS:
            groups.append(group)
            group = []
    if group:
        groups.append(group)
    out = []
    for n, g in enumerate(groups):
        start = 0.0 if n == 0 else segments[g[0]][2] / 1000
        end = segments[groups[n + 1][0]][2] / 1000 if n + 1 < len(groups) else seconds
        out.append({"words": [g[0], g[-1]], "start": round(start, 3), "end": round(end, 3)})
    return out


def check_phrases(scene: dict[str, Any]) -> None:
    """Phrases only point into the verified ayah: in order, gap-free, inside the recitation."""
    words = ayah_words(scene["body"])
    expected, last = 0, 0.0
    for item in scene["phrases"]:
        first, final = item["words"]
        if first != expected or final < first or final >= len(words) or final - first >= 6:
            raise ValueError("LEGACY_PHRASE_OUTSIDE_AYAH")
        if not last <= item["start"] < item["end"] <= scene["audio_duration"] + 0.001:
            raise ValueError("LEGACY_PHRASE_TIMING_INVALID")
        expected, last = final + 1, item["end"]
    if expected != len(words):
        raise ValueError("LEGACY_PHRASE_INCOMPLETE")
