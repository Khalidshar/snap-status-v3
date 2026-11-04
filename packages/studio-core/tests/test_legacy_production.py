"""Synthetic source fixtures are tests only. Owner samples use actual upstream bytes."""

import copy
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

import pytest

from studio_core.config import Config
from studio_core.jobs import Repository
from studio_core.network.client import NetworkResponse
from studio_core.network.egress import PolicyDenied
from studio_core.network.production import permitted
from studio_core.network.production_speech import approved
from studio_core.presentation.did_you_know import choose_fact, is_sacred, validate_content
from studio_core.production import enabled
from studio_core.production.model import DATA, ROOT, budget, canonical, resource, sha, validate
from studio_core.production.prepare import Factory, prepare, safe_xml
from studio_core.weather import content_hash


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "synthetic-test-only.wav"
    subprocess.run(  # noqa: S603 — fixed test-only FFmpeg argv
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=2",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


class Sources:
    def __init__(self, audio):
        self.audio = audio
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        if url.endswith("/reads"):
            data = []  # Exercise EveryAyah failover without pretending this is real recitation.
        elif "quranenc.com" in url:
            _, surah, ayah = url.rsplit("/", 2)
            data = {
                "result": {
                    "sura": surah,
                    "aya": ayah,
                    "translation": "تفسير تجريبي من استجابة المصدر للاختبار فقط",
                }
            }
        else:
            return NetworkResponse(200, "audio/wav", self.audio, sha(self.audio))
        raw = json.dumps(data).encode()
        return NetworkResponse(200, "application/json", raw, sha(raw))


def factory(tmp_path, key, client):
    return Factory(
        "snap-bot-v2",
        key,
        uuid.uuid4().hex,
        tmp_path / key,
        client=client,
        state=tmp_path / "ledger",
    )


def reseal(value):
    value["body"] = "\n".join(s["body"] for s in value["legacy_production"]["scenes"])
    value["fact_id"] = "legacy-" + sha(canonical(value["legacy_production"]))[:32]
    value["content_hash"] = content_hash(value)
    return value


def test_default_off(monkeypatch):
    monkeypatch.delenv("MADAR_LEGACY_PRODUCTION", raising=False)
    assert not enabled()
    monkeypatch.setenv("MADAR_LEGACY_PRODUCTION", "1")
    assert enabled()


@pytest.mark.parametrize(
    "key,roles",
    [("facts", ["fact"] * 6), ("funny", ["funny"] * 6), ("riddle", ["question", "answer"] * 3)],
)
def test_old_recipes_real_local_audio_decoding(tmp_path, audio, key, roles):
    client = Sources(audio)
    f = factory(tmp_path, key, client)
    value = f.build()
    plan = validate(value, assets=f.root)
    assert [s["role"] for s in plan["scenes"][1:-1]] == roles
    low, high, _ = budget(plan["app"])
    assert low <= plan["duration"] <= high
    assert not is_sacred(value)
    assert validate_content(value)["text"] == value["body"]
    assert plan["music"] and plan["effects"]
    if key == "riddle":
        assert len(plan["audio"]) == 3
        assert all(s["countdown"] == 5 for s in plan["scenes"] if s["role"] == "question")
        assert all(a["kind"] == "speech" for a in plan["audio"])
    assert prepare(f.app, key, f.job, f.root, client=client) == value


def test_sacred_human_only_and_verbatim_source_proof(tmp_path, audio):
    f = factory(tmp_path, "verse", Sources(audio))
    value = f.build()
    p = validate(value, assets=f.root)
    assert is_sacred(value)
    assert p["music"] is None and p["effects"] is False and p["audio"]
    assert all(a["kind"] == "human" for a in p["audio"])
    for change in ("music", "effects", "text", "tafsir", "truncated"):
        bad = copy.deepcopy(value)
        plan = bad["legacy_production"]
        verse = plan["scenes"][1]
        if change == "music":
            plan["music"] = plan["audio"][0]["file"]
        elif change == "effects":
            plan["effects"] = True
        elif change == "text":
            verse["body"] += " نص زائد"
        elif change == "tafsir":
            verse["tafsir"] = "نص آخر"
            verse["tafsir_sha256"] = sha(verse["tafsir"].encode())
        else:
            plan["audio"][0]["seconds"] = 999
        with pytest.raises(ValueError):
            validate(reseal(bad), assets=f.root)
    file = f.root / p["audio"][0]["file"]
    file.write_bytes(b"changed")
    with pytest.raises(ValueError, match="ASSET_CHANGED"):
        validate(value, assets=f.root)


def test_30_day_dedupe_is_persistent_and_app_scoped(tmp_path, audio):
    one = factory(tmp_path, "facts", Sources(audio))
    first = one.build()
    two = Factory(
        one.app,
        "facts",
        uuid.uuid4().hex,
        tmp_path / "second",
        client=Sources(audio),
        state=tmp_path / "ledger",
    )
    second = two.build()
    assert {s["body"] for s in first["legacy_production"]["scenes"][1:-1]}.isdisjoint(
        s["body"] for s in second["legacy_production"]["scenes"][1:-1]
    )
    with pytest.raises(ValueError, match="HISTORY_SCOPE"):
        Factory(
            "status-bot-v2",
            "facts",
            uuid.uuid4().hex,
            tmp_path / "wrong-app",
            client=Sources(audio),
            state=tmp_path / "ledger",
        )


def test_asset_paths_xml_and_network_fail_closed(tmp_path):
    for name in ("/etc/passwd", "assets/../x", "assets/link/x", "assets/a?x"):
        with pytest.raises(ValueError):
            resource(tmp_path, name, "a" * 64)
    for url in (
        "https://127.0.0.1/a",
        "http://mp3quran.net/a",
        "https://mp3quran.net.evil.test/a",
        "https://unknown.example/a",
    ):
        with pytest.raises((ValueError, OSError)):
            permitted(url)
    for raw in (b"<!DOCTYPE rss><rss/>", b'<!ENTITY x "x"><rss/>', b"X" * 4_000_001):
        with pytest.raises(ValueError):
            safe_xml(raw)
    assert safe_xml(b"<rss><channel><item/></channel></rss>").find(".//item") is not None


def test_free_speech_cannot_synthesize_sacred_recitation():
    q = json.loads((DATA / "riddles.json").read_bytes())[0]
    assert approved("tts://riddle?" + urlencode({"text": q["a"]})) == q["a"]
    for url in [
        "tts://verse?text=x",
        "tts://adhkar?text=x",
        "tts://riddle?text=arbitrary",
        "tts://hadith?text=arbitrary",
    ]:
        with pytest.raises(PolicyDenied):
            approved(url)


def test_owned_source_route_and_cached_reservation(tmp_path, audio, monkeypatch):
    monkeypatch.setenv("MADAR_LEGACY_PRODUCTION", "1")
    from studio_core.production import pipeline

    monkeypatch.setattr(pipeline, "source_root", lambda app, job: tmp_path / "source" / job)
    monkeypatch.setattr(
        pipeline,
        "prepare",
        lambda app, key, job, root: prepare(
            app, key, job, root, client=Sources(audio), state=tmp_path / "ledger"
        ),
    )
    config = replace(Config.load(ROOT / "config/snap/weather.toml"), data_root=tmp_path / "state")
    repo = Repository(config)
    job, _ = repo.enqueue(
        "legacy-test", "synthetic-test", "did_you_know", "NORMAL", "input", repo.clock() + 3600
    )
    claim = repo.claim(job_id=job["id"])
    assert claim
    value, _ = choose_fact(repo, job["id"], claim["lease_token"], claim["attempt"], kind="facts")
    again, _ = choose_fact(repo, job["id"], claim["lease_token"], claim["attempt"], kind="facts")
    assert value == again
    target = tmp_path / "staged/content.json"
    target.parent.mkdir()
    target.write_bytes(canonical(value))
    pipeline.stage(value, config.app, job["id"], target)
    assert (target.parent / "legacy.json").is_file()
    validate(value, assets=target.parent)


@pytest.mark.skipif(
    os.getenv("MADAR_PRODUCTION_REAL_RENDER") != "1", reason="dedicated Chromium job"
)
def test_actual_offline_chromium_frame_audio_and_six_shot_judge(tmp_path, audio):
    f = factory(tmp_path, "facts", Sources(audio))
    f.build()
    out = tmp_path / "output"
    run = subprocess.run(  # noqa: S603 — test-only offline renderer
        [sys.executable, "-m", "studio_core.production.render", str(f.root), str(out)],
        check=False,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert run.returncode == 0, run.stderr[-6000:]
    result = json.loads((out / "report.json").read_bytes())
    assert result["qa"]["result"] == "PASS" and result["external_delivery"] is False
    assert all(result["network_isolation"].values())
    assert result["qa"]["audio"] == "LICENSED_MUSIC"
    assert result["video_sha256"] == hashlib.sha256((out / "video.mp4").read_bytes()).hexdigest()
    assert len(result["qa"]["judge"]["shots"]) == 6
    assert result["brand"]["clip_box"] == {"x": 108, "y": 210, "width": 864, "height": 1536}
    assert "PASS" in run.stdout


@pytest.fixture
def picture(tmp_path):
    path = tmp_path / "test-only.jpg"
    subprocess.run(  # noqa: S603 — fixed test argv
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=80x80",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


@pytest.mark.parametrize("key", ["news", "tech"])
def test_bulletin_and_article_points_have_actual_source_assets(tmp_path, audio, picture, key):
    import email.utils

    from studio_core.sources import for_command

    source = next(s for s in for_command(key) if s.id == ("bbc-ar" if key == "news" else "aitnews"))
    published = email.utils.formatdate(time.time() - 60, usegmt=True)
    host = "https://www.bbc.com/arabic" if key == "news" else "https://aitnews.com"
    title = "أخبار السعودية والتقنية الجديدة"
    paragraph = "تطور جديد في المملكة العربية السعودية والتقنية الحديثة ومعلومات من المصدر الرسمي. "
    rss = (
        "<rss><channel>"
        + "".join(
            f"<item><title>{title} {n}</title><link>{host}/story{n}</link>"
            f"<pubDate>{published}</pubDate><description>{paragraph}</description></item>"
            for n in range(3)
        )
        + "</channel></rss>"
    )
    page = (
        '<meta property="og:image" content="https://ichef.bbci.co.uk/test.jpg"><article>'
        + "".join(f"<h2>تفاصيل الخبر {n}</h2><p>{n} {paragraph * 3}</p>" for n in range(4))
        + "</article>"
    )

    class Feed(Sources):
        def get(self, url):
            if url == source.url:
                raw = rss.encode()
            elif "/story" in url:
                raw = page.encode()
            elif url == "https://ichef.bbci.co.uk/test.jpg":
                raw = picture
            elif "free-stock-music.com/music/" in url:
                raw = audio
            else:
                raise RuntimeError("Synthetic fixture unavailable")
            return NetworkResponse(200, "", raw, sha(raw))

    f = factory(tmp_path, key, Feed(audio))
    value = f.build()
    plan = validate(value, assets=f.root)
    core = plan["scenes"][:-1]
    assert core[0]["at"] == 0 and core[0].get("image")
    assert all(s.get("image") and s.get("source") and s.get("published_label") for s in core)
    if key == "news":
        assert len(core) == 3 and all(s["role"] == "story" for s in core)
        # Snap pads to its 61 s floor evenly: every story keeps the same pace, at least 12 s.
        assert len({round(s["seconds"], 3) for s in core}) == 1 and core[0]["seconds"] >= 12
    else:
        assert core[0]["role"] == "headline" and all(s["role"] == "point" for s in core[1:])
    broken = copy.deepcopy(value)
    broken["legacy_production"]["scenes"][1]["published"] = 0
    with pytest.raises(ValueError, match="SOURCE_OR_TIME"):
        validate(reseal(broken), assets=f.root)


def test_daily_brief_uses_both_weather_providers_and_fails_on_disagreement(tmp_path, audio):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Riyadh"))

    class Brief(Sources):
        disagree = False

        def get(self, url):
            if "timingsByCity" in url:
                data = {
                    "data": {
                        "timings": dict.fromkeys(
                            ("Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"), "05:15"
                        ),
                        "date": {"hijri": {"year": "1448"}},
                    }
                }
            elif "/hToG/" in url:
                data = {"data": {"gregorian": {"date": f"01-01-{today.year + 1}"}}}
            elif "api.open-meteo.com" in url:
                data = {"daily": {"temperature_2m_max": [28], "temperature_2m_min": [18]}}
            elif "api.met.no" in url:
                data = {
                    "properties": {
                        "timeseries": [
                            {
                                "time": today.isoformat(),
                                "data": {
                                    "instant": {
                                        "details": {
                                            "air_temperature": v + (10 if self.disagree else 0)
                                        }
                                    }
                                },
                            }
                            for v in (18, 28)
                        ]
                    }
                }
            else:
                return super().get(url)
            raw = json.dumps(data).encode()
            return NetworkResponse(200, "application/json", raw, sha(raw))

    client = Brief(audio)
    f = factory(tmp_path, "brief", client)
    value = f.build()
    p = validate(value, assets=f.root)
    assert len(p["scenes"][1:-1]) == 4 and p["audio"] and not p["music"]
    assert "ص" in p["scenes"][1]["body"]
    client.disagree = True
    other = Factory(
        "snap-bot-v2",
        "brief",
        uuid.uuid4().hex,
        tmp_path / "disagree",
        client=client,
        state=tmp_path / "ledger",
    )
    with pytest.raises(ValueError, match="WEATHER_DISAGREES"):
        other.build()


def test_each_dhikr_is_bound_to_its_human_recording_and_count(tmp_path, audio):
    recording = tmp_path / "synthetic-dhikr-test-only.wav"
    subprocess.run(  # noqa: S603 — fixed local test-only audio argv
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=8",
            str(recording),
        ],
        check=True,
    )
    audio = recording.read_bytes()
    rows = [
        {
            "ID": i,
            "ARABIC_TEXT": "ذكر تجريبي للاختبار فقط " + str(i),
            "REPEAT": 3,
            "AUDIO": f"http://www.hisnmuslim.com/audio/ar/{i}.mp3",
        }
        for i in range(1, 24)
    ]

    class Recording(Sources):
        def get(self, url):
            if url.endswith("/27.json"):
                raw = json.dumps({"أذكار الصباح والمساء": rows}).encode()
                return NetworkResponse(200, "application/json", raw, sha(raw))
            return super().get(url)

    f = factory(tmp_path, "adhkar_am", Recording(audio))
    value = f.build()
    p = validate(value, assets=f.root)
    segments = p["scenes"][1:-1]
    # Selection window is on the recordings; Snap may then hold each card longer.
    assert 44 <= sum(s["audio_duration"] + 0.3 for s in segments) <= 50
    assert all(s["seconds"] >= s["audio_duration"] + 0.3 - 1e-9 for s in segments)
    assert all(s["body"] == rows[s["dhikr_id"] - 1]["ARABIC_TEXT"] for s in segments)
    assert all(s["number"] == "التكرار: ٣" for s in segments)
    assert all(s["levels"] and max(s["levels"]) > 0 for s in segments)
    assert p["music"] is None and not p["effects"]
    for field in ("body", "number", "audio"):
        changed = copy.deepcopy(value)
        changed["legacy_production"]["scenes"][1][field] = "changed"
        with pytest.raises(ValueError):
            validate(reseal(changed), assets=f.root)


def test_production_broker_redirect_and_mime_guards(monkeypatch, tmp_path):
    from urllib.parse import urlsplit

    from studio_core.network import production

    calls = []

    class Reply:
        status = 200
        mime = "image/jpeg"

        def getheader(self, name, default=None):
            return self.mime if name == "Content-Type" else default

        def read(self, n):
            return b"jpeg fixture"

    class Connection:
        def __init__(self, host, address, timeout):
            calls.append((host, address, timeout))

        def request(self, *args, **kw):
            pass

        def getresponse(self):
            return Reply()

        def close(self):
            pass

    monkeypatch.setattr(production, "permitted", lambda url: urlsplit(url).hostname)
    monkeypatch.setattr(
        production, "validate_target", lambda url, hosts: (hosts[0], "8.8.8.8", "/image")
    )
    monkeypatch.setattr(production, "PinnedHTTPSConnection", Connection)
    policy = production.ProductionPolicy(tmp_path / "unused.sock")
    assert production.fetch("https://mp3quran.net/image", policy)["status"] == 200
    assert calls == [("mp3quran.net", "8.8.8.8", 90)]
    Reply.mime = "application/javascript"
    with pytest.raises(PolicyDenied, match="MIME_DENIED"):
        production.fetch("https://mp3quran.net/image", policy)


@pytest.mark.parametrize("app", ["snap-bot-v2", "status-bot-v2"])
def test_source_image_falls_back_to_og_before_google(tmp_path, monkeypatch, app):
    f = Factory(
        app,
        "urgent",
        uuid.uuid4().hex,
        tmp_path / "input",
        client=object(),
        state=tmp_path / "state",
    )
    attempts = []

    def picture(url, base, credit):
        attempts.append(url)
        if url == "https://publisher.example/broken.jpg":
            raise ValueError("unavailable")
        return "assets/source.jpg"

    monkeypatch.setattr(f, "picture", picture)
    result = f.news_picture(
        {
            "image": "https://publisher.example/broken.jpg",
            "og_image": "https://publisher.example/actual.jpg",
            "url": "https://publisher.example/story",
            "source": "المصدر",
        }
    )
    assert result == "assets/source.jpg"
    assert attempts == [
        "https://publisher.example/broken.jpg",
        "https://publisher.example/actual.jpg",
    ]


def test_article_extraction_keeps_complete_source():
    from studio_core.production.parsing import body_text

    text = "معلومة موثقة من المصدر " * 400
    assert len(body_text("<article><p>" + text + "</p></article>")) > 6000


def test_translation_checks_source_echo_language_and_numbers():
    from studio_core.production.translation import decode

    raw = json.dumps(
        [[["أطلقت شانيل 3 عطور في 2027.", "Chanel launched 3 perfumes in 2027."]]]
    ).encode()
    assert "2027" in decode("Chanel launched 3 perfumes in 2027.", raw)
    with pytest.raises(ValueError, match="FACTS"):
        decode(
            "Chanel launched 4 perfumes in 2027.",
            json.dumps(
                [[["أطلقت شانيل 3 عطور في 2027.", "Chanel launched 4 perfumes in 2027."]]]
            ).encode(),
        )
    with pytest.raises(ValueError, match="ECHO"):
        decode("Another article.", raw)


@pytest.mark.parametrize("app", ["snap-bot-v2", "status-bot-v2"])
def test_english_article_translation_is_bound_to_full_source(tmp_path, audio, picture, app):
    import email.utils
    from urllib.parse import parse_qs, urlsplit

    from studio_core.sources import for_command

    source = next(s for s in for_command("style") if s.id == "vogue")
    title = "Chanel fashion collection 2027"
    paragraph = (
        "The designer presented a new fashion collection with elegant dresses "
        "and detailed fabrics at the runway show. "
    )
    page = (
        '<meta property="og:image" content="https://www.vogue.com/picture.jpg"><article>'
        + "".join(
            f"<h2>Fashion collection {i}</h2><p>Series {i}: {paragraph * 4}</p>" for i in range(3)
        )
        + "</article>"
    )
    published = email.utils.formatdate(time.time() - 60, usegmt=True)
    rss = (
        f"<rss><channel><item><title>{title}</title>"
        "<link>https://www.vogue.com/article/test</link>"
        f"<pubDate>{published}</pubDate><description>{paragraph}</description>"
        "</item></channel></rss>"
    )

    class English(Sources):
        def get(self, url):
            if url == source.url:
                raw = rss.encode()
            elif url == "https://www.vogue.com/article/test":
                raw = page.encode()
            elif url == "https://www.vogue.com/picture.jpg":
                raw = picture
            elif "translate.googleapis.com" in url:
                text = parse_qs(urlsplit(url).query)["q"][0]
                import re

                nums = re.findall(r"\d+", text)
                target = "مجموعة أزياء شانيل وتفاصيل الأقمشة والفساتين الجديدة " + " ".join(nums)
                raw = json.dumps([[[target, text]]]).encode()
            elif "free-stock-music.com/music/" in url:
                raw = audio
            else:
                raise RuntimeError("fixture source unavailable")
            return NetworkResponse(200, "", raw, sha(raw))

    f = Factory(
        app,
        "style",
        uuid.uuid4().hex,
        tmp_path / "input",
        client=English(audio),
        state=tmp_path / "state",
    )
    value = f.build()
    p = validate(value, assets=f.root)
    assert p["scenes"][0]["at"] == 0
    assert all(s.get("translations") for s in p["scenes"][:-1])
    assert (
        len(
            next(
                (f.root / a["file"]).read_text()
                for a in p["assets"]
                if a["source_url"] == "https://www.vogue.com/article/test"
            )
        )
        > 1200
    )
    changed = copy.deepcopy(value)
    changed["legacy_production"]["scenes"][1]["body"] += " خبر غير موجود بالمصدر"
    with pytest.raises(ValueError, match="TRANSLATION_DISPLAY_CHANGED"):
        validate(reseal(changed), assets=f.root)


def test_top_brand_priority_is_case_insensitive_and_not_a_fashion_limit():
    from studio_core.production.prepare import Factory
    from studio_core.sources.news import top_brand

    assert top_brand("style", "CHANEL fashion week")
    assert top_brand("style", "إطلالة من ديور")
    assert Factory.editorial_match("style", "Independent designer runway fashion", "en")
    assert not Factory.editorial_match("style", "Stock market report", "en")


def test_bundled_amiri_covers_every_locked_quran_character():
    from studio_core.presentation.faith import _subset
    from studio_core.production.render import verify_fonts

    verify_fonts(
        {"scenes": [{"body": text, "verse": [s, a]} for (s, a), text in _subset()[0].items()]}
    )


@pytest.mark.parametrize("app", ["snap-bot-v2", "status-bot-v2"])
def test_isha_production_dispatch_and_all_content_commands_cover_both_bots(monkeypatch, app):
    from snap_bot import owner_commands as snap
    from status_bot import content_clips
    from status_bot import owner_commands as status
    from studio_core import commands
    from studio_core.production.model import TITLES

    for c in commands.COMMANDS:
        if c.section in {"news", "general", "faith", "brief"}:
            assert c.key in TITLES
    monkeypatch.setenv("MADAR_LEGACY_PRODUCTION", "1")
    assert status.production_content("isha")
    assert "isha" in content_clips.KINDS and "isha" in snap.RENDER_KINDS
    assert content_clips.KINDS["isha"] == snap.RENDER_KINDS["isha"] == "verse"


@pytest.mark.parametrize("app", ["snap-bot-v2", "status-bot-v2"])
def test_night_number_and_verified_hadith_in_both_bots(tmp_path, monkeypatch, audio, app):
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    f = Factory(
        app,
        "night",
        uuid.uuid4().hex,
        tmp_path / "input",
        client=Sources(audio),
        state=tmp_path / "state",
    )
    monkeypatch.setattr(
        f,
        "adhkar",
        lambda *a, **kw: [
            {
                "body": "نص تجريبي لاختبار ترقيم الليالي",
                "seconds": 9.0,
                "role": "sleep_reminder",
                "audio": f.save(audio, ".mp3", "https://archive.org/test", "حصن المسلم"),
                "audio_duration": 2.0,
            }
        ],
    )
    value = f.build()
    p = validate(value, assets=f.root)
    assert (
        p["night_number"]
        == (datetime.now(ZoneInfo("Asia/Riyadh")).date() - date(2026, 9, 26)).days + 1
    )
    assert not p["music"] and not p["effects"]
    assert any(s.get("grade") == "صحيح" for s in p["scenes"] if s["role"] == "hadith")
    changed = copy.deepcopy(value)
    changed["legacy_production"]["night_number"] += 1
    with pytest.raises(ValueError, match="NIGHT_NUMBER"):
        validate(reseal(changed))


@pytest.mark.parametrize("app,config_name", [("snap-bot-v2", "snap"), ("status-bot-v2", "status")])
def test_six_oversight_roles_are_durable_for_both_source_pipelines(
    tmp_path, monkeypatch, audio, app, config_name
):
    from studio_core.production import pipeline
    from studio_core.production.oversight import ROLES

    monkeypatch.setenv("MADAR_LEGACY_PRODUCTION", "1")
    f = Factory(
        app,
        "facts",
        uuid.uuid4().hex,
        tmp_path / "input",
        client=Sources(audio),
        state=tmp_path / "state",
    )
    value = f.build()
    config = replace(
        Config.load(ROOT / f"config/{config_name}/weather.toml"),
        data_root=tmp_path / "jobs",
        command_allowlist=("did_you_know",),
    )
    repo = Repository(config)
    job, _ = repo.enqueue("source", "owner", "did_you_know", "NORMAL", "input", repo.clock() + 1200)
    claim = repo.claim(job_id=job["id"])
    repo.transition(job["id"], claim["lease_token"], "RESEARCHING")
    monkeypatch.setattr(pipeline, "prepare", lambda *a: value)
    monkeypatch.setattr(pipeline, "source_root", lambda *a: f.root)
    pipeline.choose(repo, job["id"], claim["lease_token"], claim["attempt"], "facts")
    with repo.connect() as db:
        rows = db.execute(
            "SELECT role,status FROM agent_runs WHERE job_id=?", (job["id"],)
        ).fetchall()
    assert {r["role"] for r in rows if r["status"] == "SUCCEEDED"} == set(ROLES)


@pytest.mark.parametrize("app,short", [("snap-bot-v2", "snap"), ("status-bot-v2", "status")])
def test_foundation_is_optional_third_human_provider_and_tracks_retention(
    tmp_path, monkeypatch, audio, app, short
):
    import importlib

    from studio_core.religious import quran_foundation, quran_pipeline

    module = importlib.import_module("studio_core.production.prepare")
    monkeypatch.setenv("MADAR_QURAN_AUDIO", "1")
    clock = [time.time()]
    store = quran_foundation.Store(tmp_path / ("quran-" + short), app, clock=lambda: clock[0])
    monkeypatch.setattr(quran_pipeline, "store_for", lambda scope: store)

    class Broker:
        def get(self, url):
            raw = json.dumps({"configured": True, "environment": "production"}).encode()
            return NetworkResponse(200, "application/json", raw, sha(raw))

    class Foundation:
        def __init__(self, client, environment):
            assert environment == "production"

        def catalog(self, policy):
            return {"reciters": [{"id": 999, "name": "قارئ جديد"}]}

        def audio(self, verse, reader):
            assert verse == "94:5" and reader["id"] == 999
            return audio

    monkeypatch.setattr(module, "NetworkClient", lambda config: Broker())
    monkeypatch.setattr(quran_foundation, "Source", Foundation)

    class FailedPrimary(Sources):
        def get(self, url):
            if "quranenc.com" in url or url.endswith("/reads"):
                return super().get(url)
            raise RuntimeError("keyless sources unavailable in fixture")

    f = Factory(
        app,
        "verse",
        uuid.uuid4().hex,
        tmp_path / short / "production/payloads" / uuid.uuid4().hex,
        client=FailedPrimary(audio),
        state=tmp_path / short / "state",
    )
    scenes = f.quran(94, [5])
    assert "مؤسسة القرآن" in scenes[0]["source"] and "قارئ جديد" in scenes[0]["source"]
    asset = next(a for a in f.assets if a.get("qf_job"))
    copied = f.root / asset["file"]
    assert copied.is_file()
    clock[0] += 7 * 86400
    store.sweep()
    assert not copied.exists()
    assert store.db.exists()  # Rotation and audit evidence survive the media cleanup.


def test_quran_rotation_never_repeats_same_human_across_provider_ids(tmp_path, monkeypatch, audio):
    f = factory(tmp_path, "verse", Sources(audio))
    first = f.quran(94, [5])[0]["source"]
    second = Factory(
        "snap-bot-v2",
        "verse",
        uuid.uuid4().hex,
        tmp_path / "next",
        client=Sources(audio),
        state=tmp_path / "ledger",
    )
    assert second.quran(94, [5])[0]["source"] != first


def test_article_cannot_escape_its_approved_publisher(tmp_path, audio):
    f = factory(tmp_path, "news", Sources(audio))
    with pytest.raises(ValueError, match="Foreign or unsafe"):
        f.article({"source_id": "bbc-ar", "url": "https://archive.org/injected"})


def test_production_caption_keeps_credits_and_article_links(tmp_path, audio):
    from studio_core.production.caption import caption, platform_caption

    f = factory(tmp_path, "facts", Sources(audio))
    value = f.build()
    description = platform_caption(value)
    assert "@rawwag_khalid" in description and "True Patriot" in description
    assert "creativecommons.org/licenses/by-sa/3.0/" in description
    assert "True Patriot" in caption(value)
    changed = copy.deepcopy(value)
    changed["legacy_production"]["scenes"][0]["source"] = "مصدر طويل " * 200
    with pytest.raises(ValueError, match="CAPTION_TOO_LONG"):
        platform_caption(reseal(changed))


def test_amiri_addition_preserves_all_existing_license_pins():
    manifest = json.loads((ROOT / "assets/fonts/manifest.json").read_bytes())
    for font in manifest["fonts"]:
        for license in font.get("licenses", []):
            assert sha((ROOT / "assets/fonts" / license["file"]).read_bytes()) == license["sha256"]


@pytest.mark.parametrize(
    "metric,value",
    [
        ("integrated_lufs", -18.01),
        ("integrated_lufs", -10.99),
        ("true_peak_dbtp", -0.99),
        ("height", 1080),
        ("duration", 31.99),
        (None, None),
    ],
)
def test_independent_final_qa_rejects_bad_audio_or_video_claims(
    tmp_path, audio, monkeypatch, metric, value
):
    from types import SimpleNamespace

    from studio_core.presentation.brand_frame import load_brand
    from studio_core.production import pipeline, render

    f = factory(tmp_path, "facts", Sources(audio))
    content = f.build()
    plan = content["legacy_production"]
    video = tmp_path / "test-only-probe-fixture.mp4"
    video.write_bytes(b"not-a-real-owner-sample")
    probe = {
        "streams": [
            {
                "codec_type": "video",
                "width": 1080,
                "height": 1920,
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "r_frame_rate": "30/1",
            },
            {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
        ],
        "format": {"duration": str(plan["duration"])},
    }
    measurements = {"integrated_lufs": -16.0, "true_peak_dbtp": -1.5}
    if metric in measurements:
        measurements[metric] = value
    elif metric == "height":
        probe["streams"][0]["height"] = value
    elif metric == "duration":
        probe["format"]["duration"] = str(value)

    def tool(argv, timeout):
        if "-show_streams" in argv:
            return SimpleNamespace(stdout=json.dumps(probe), stderr="")
        if "-frames:v" in argv:
            Path(argv[-1]).write_bytes(b"test-only-shot")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(render, "run", tool)
    monkeypatch.setattr(render, "measure_audio", lambda *a: measurements)
    layout = {"overflow": 0, "empty": 0, "tiny": 0, "handle": True}
    if metric is not None:
        with pytest.raises(ValueError, match="LEGACY_QA"):
            render.qa(video, plan, layout, tmp_path)
    else:
        qa = render.qa(video, plan, layout, tmp_path)
        assert qa["judge"]["score"] == 10 and len(qa["judge"]["criteria"]) == 10
        brand = load_brand()
        result = {
            "content_hash": content["content_hash"],
            "qa": qa,
            "legacy_production": {
                "content": content,
                "brand": {
                    "clip_box": brand["clip_box"],
                    "overlay_sha256": brand["overlay"]["sha256"],
                },
            },
        }
        assert pipeline.check_result(result) == plan["duration"]
        result["qa"]["judge"]["criteria"]["audio"] = False
        with pytest.raises(ValueError, match="JUDGE_CHANGED"):
            pipeline.check_result(result)
