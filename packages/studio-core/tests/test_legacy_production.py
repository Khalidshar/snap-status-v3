"""Synthetic source fixtures are tests only. Owner samples use actual upstream bytes."""

import copy
import hashlib
import json
import os
import subprocess
import sys
import uuid
from dataclasses import replace
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
from studio_core.production.model import DATA, ROOT, canonical, resource, sha, validate
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
    [("facts", ["fact"] * 4), ("funny", ["funny"] * 4), ("riddle", ["question", "answer"] * 3)],
)
def test_old_recipes_real_local_audio_decoding(tmp_path, audio, key, roles):
    client = Sources(audio)
    f = factory(tmp_path, key, client)
    value = f.build()
    plan = validate(value, assets=f.root)
    assert [s["role"] for s in plan["scenes"][1:-1]] == roles
    assert 32 <= plan["duration"] <= 58
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
        check=True,
        capture_output=True,
        text=True,
        timeout=1800,
    )
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
    published = email.utils.formatdate(usegmt=True)
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
    core = plan["scenes"][1:-1]
    assert all(s.get("image") and s.get("source") and s.get("published_label") for s in core)
    if key == "news":
        assert len(core) == 3 and all(s["role"] == "story" for s in core)
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


def test_real_audio_pause_detection_and_rms_bars(tmp_path, audio):
    full = tmp_path / "synthetic-adhkar-test-only.wav"
    subprocess.run(  # noqa: S603 — fixed test argv
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=55",
            "-af",
            "volume='if(between(t,45,48),0,0.5)':eval=frame",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(full),
        ],
        check=True,
    )

    class Recording(Sources):
        def get(self, url):
            if "archive.org" in url:
                raw = full.read_bytes()
                return NetworkResponse(200, "audio/wav", raw, sha(raw))
            return super().get(url)

    f = factory(tmp_path, "adhkar_am", Recording(audio))
    value = f.build()
    p = validate(value, assets=f.root)
    segment = p["scenes"][1]
    assert 44 <= segment["audio_duration"] <= 50
    assert len(segment["levels"]) > 600 and max(segment["levels"]) > 0
    assert p["music"] is None and not p["effects"]


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
