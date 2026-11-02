"""QF contract tests use invented non-scriptural text, never fabricated tafsir."""

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from studio_core.network.quran import QuranEgress, QuranPolicy
from studio_core.presentation import faith
from studio_core.presentation.quran_clip import pages
from studio_core.religious import quran_foundation as qf

APP = "snap-bot-v2"
REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def catalog(tmp_path):
    value = json.loads(qf.CATALOG.read_text())
    value["approved_by"] = "خالد"
    path = tmp_path / "approved.json"
    path.write_text(json.dumps(value, ensure_ascii=False))
    return path


class FixtureClient:
    """Shape follows the documented Content Sync response, not the regular API."""

    def __init__(self, *, missing_muyassar=False, audio=b"invalid", group=False):
        self.urls = []
        self.missing_muyassar, self.audio, self.group = missing_muyassar, audio, group

    def get(self, url):
        self.urls.append(url)
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/resources/tafsirs"):
            value = {
                "tafsirs": [
                    {"id": 16, "slug": "ar-tafsir-muyassar", "language_name": "arabic"},
                    {"id": 170, "slug": "ar-tafseer-al-saddi", "language_name": "arabic"},
                ]
            }
        elif parsed.path.endswith("/resources/recitations"):
            value = {"recitations": [{"id": 7, "translated_name": {"name": "مشاري العفاسي"}}]}
        elif parsed.path.endswith("/resources/sync"):
            group, identifier = query["resources"][0].split(":")
            mutations = (
                []
                if self.missing_muyassar and identifier == "16"
                else [
                    {
                        "type": "RESOURCE_CREATE",
                        "resource_group": group,
                        "resource_id": int(identifier),
                        "snapshot_url": f"/api/v4/resources/snapshots/{group}/{identifier}",
                    }
                ]
            )
            value = {
                "sync": {
                    "has_more": False,
                    "next_page_url": None,
                    "next_sync_token": "fixture-checkpoint",
                    "mutations": mutations,
                }
            }
        elif "/snapshots/" in parsed.path:
            group, identifier = parsed.path.split("/")[-2:]
            record = (
                {
                    "verse_key": "2:45",
                    "text": "نص اختباري لقياس الصفحة فقط، وليس تفسيراً.",
                    "group_verse_key_from": "2:45",
                    "group_verse_key_to": "2:46" if self.group else "2:45",
                }
                if group == "tafsirs"
                else {
                    "record_type": "audio_file",
                    "recitation_id": 7,
                    "verse_key": "2:45",
                    "url": "https://verses.quran.com/fixture.mp3",
                }
            )
            value = {
                "schema_version": 1,
                "resource_group": group,
                "resource_id": int(identifier),
                "records": [record],
            }
        else:
            return SimpleNamespace(body=self.audio)
        return SimpleNamespace(body=json.dumps(value, ensure_ascii=False).encode())


def fixture_bundle(tmp_path, catalog, *, app=APP, client=None):
    store = qf.Store(tmp_path / app, app)
    source = qf.Source(client or FixtureClient())
    path = qf.prepare(store, source, "a" * 32, 2, 45, catalog)
    return store, path


def test_king_fahd_text_is_exact_and_never_requested_from_qf(tmp_path, catalog):
    client = FixtureClient()
    store, path = fixture_bundle(tmp_path, catalog, client=client)
    data = qf.verify_bundle(path, APP, store.clock())
    original = next(
        r
        for r in json.loads((faith.DATA / "quran_subset.json").read_bytes())["items"]
        if (r["surah"], r["ayah"]) == (2, 45)
    )
    assert data["text"].encode() == original["text"].encode()
    assert data["text_sha256"] == original["sha256"]
    assert all("/verses/" not in u and "/quran/" not in u for u in client.urls)
    data["text"] += " "
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="QF_TEXT_CHANGED"):
        qf.verify_bundle(path, APP, store.clock())


def test_proposals_cannot_run(tmp_path):
    store = qf.Store(tmp_path, APP)
    with pytest.raises(ValueError, match="AWAIT_OWNER_APPROVAL"):
        store.reserve("a" * 32, json.loads(qf.CATALOG.read_text()))
    assert not list(store.payload.iterdir())


def test_rotation_is_atomic_and_retries_keep_reader(tmp_path, catalog):
    store = qf.Store(tmp_path, APP)
    approved = json.loads(catalog.read_text())
    jobs = [f"{i:032x}" for i in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda j: store.reserve(j, approved), jobs))
    with sqlite3.connect(store.db) as db:
        rows = db.execute("SELECT job,reciter FROM clips ORDER BY rowid").fetchall()
    assert all(a[1] != b[1] for a, b in pairwise(rows))
    for job, reciter in rows:
        assert store.reserve(job, approved)[0]["id"] == reciter


def test_bots_have_independent_ledgers_and_cannot_share_root(tmp_path, catalog):
    a = qf.Store(tmp_path / "snap", APP)
    b = qf.Store(tmp_path / "status", "status-bot-v2")
    approved = json.loads(catalog.read_text())
    assert a.reserve("a" * 32, approved)[0] == b.reserve("a" * 32, approved)[0]
    with pytest.raises(ValueError, match="APP_ROOT_SHARED"):
        qf.Store(a.root, "status-bot-v2")


def test_cleanup_at_seven_days_including_derived_artifacts_but_not_logs(tmp_path, catalog):
    now = [100.0]
    store = qf.Store(tmp_path, APP, lambda: now[0])
    _, expires = store.reserve("a" * 32, json.loads(catalog.read_text()))
    directory = store.directory("a" * 32)
    for name in ("recitation.mp3", "bundle.json", "sample.png", "video.mp4"):
        (directory / name).write_bytes(b"fixture")
    audit = tmp_path / "events.jsonl"
    audit.write_bytes(b"immutable log\n")
    now[0] = expires - 1
    assert store.sweep() == 0 and directory.exists()
    now[0] = 100 + qf.WEEK
    assert store.sweep() == 1 and not directory.exists()
    assert audit.read_bytes() == b"immutable log\n" and store.db.exists()
    with sqlite3.connect(store.db) as db:
        assert db.execute("SELECT state FROM clips").fetchone() == ("PURGED",)


def test_no_audio_on_corrupt_file_and_no_retry_with_other_reader(tmp_path, catalog):
    client = FixtureClient(audio=b"broken mp3")
    store, path = fixture_bundle(tmp_path, catalog, client=client)
    value = qf.verify_bundle(path, APP, store.clock())
    assert value["audio_state"] == "SILENT_AUDIO_FAILED"
    assert value["audio_duration"] is None and value["audio_sha256"] is None
    assert not (path.parent / "recitation.mp3").exists()
    assert len([u for u in client.urls if u.endswith(".mp3")]) == 1
    assert value["attribution"] == "التلاوة: مشاري العفاسي — المصدر: مؤسسة القرآن"


def test_tafsir_falls_back_to_saadi_and_names_actual_source(tmp_path, catalog):
    _, path = fixture_bundle(tmp_path, catalog, client=FixtureClient(missing_muyassar=True))
    value = json.loads(path.read_bytes())
    assert value["tafsir"]["name"] == "تفسير السعدي"
    assert value["tafsir"]["resource_id"] == 170


def test_grouped_tafsir_cannot_be_excerpted_as_one_verse():
    with pytest.raises(ValueError, match="WHOLE_CONTEXT"):
        qf.Source(FixtureClient(group=True)).tafsir("2:45")


def test_tafsir_markup_is_literal_and_unsupported_content_rejected():
    assert qf.literal_tafsir("<p>نص &amp; نص</p><p>فقرة</p>") == "نص & نص\nفقرة"
    with pytest.raises(ValueError, match="MARKUP_UNSUPPORTED"):
        qf.literal_tafsir("<script>نص</script>")


def test_tafsir_hash_tamper_is_blocked(tmp_path, catalog):
    store, path = fixture_bundle(tmp_path, catalog)
    value = json.loads(path.read_bytes())
    value["tafsir"]["text"] += " تغيير"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="TEXT_CHANGED"):
        qf.verify_bundle(path, APP, store.clock())


def test_expired_or_other_bot_bundle_cannot_render(tmp_path, catalog):
    store, path = fixture_bundle(tmp_path, catalog)
    value = json.loads(path.read_bytes())
    with pytest.raises(ValueError, match="CLIP_EXPIRED"):
        qf.verify_bundle(path, APP, value["expires_at"])
    with pytest.raises(ValueError, match="SCOPE_CHANGED"):
        qf.verify_bundle(path, "status-bot-v2", store.clock())


def test_same_job_cannot_change_verse(tmp_path, catalog):
    store, _ = fixture_bundle(tmp_path, catalog)
    with pytest.raises(ValueError, match="JOB_CONTENT_CHANGED"):
        qf.prepare(store, qf.Source(FixtureClient()), "a" * 32, 2, 152, catalog)


def test_prelive_stays_in_approved_dataset(tmp_path, catalog):
    store = qf.Store(tmp_path, APP)
    with pytest.raises(ValueError, match="PRELIVE_SURAH"):
        qf.prepare(store, qf.Source(FixtureClient()), "a" * 32, 3, 8, catalog)


@pytest.mark.parametrize("relative", ["Alafasy/mp3/002045.mp3", "/Alafasy/mp3/002045.mp3"])
def test_documented_relative_audio_paths_use_only_qf_cdn(relative):
    client = FixtureClient(audio=b"codec-fixture")
    source = qf.Source(client)
    original = source.snapshot

    def snapshot(group, identifier):
        rows = original(group, identifier)
        rows[0]["url"] = relative
        return rows

    source.snapshot = snapshot
    assert source.audio("2:45", {"id": 7, "name": "مشاري العفاسي"}) == b"codec-fixture"
    assert client.urls[-1] == "https://verses.quran.com/Alafasy/mp3/002045.mp3"


def test_pagination_restores_all_content_and_paragraphs():
    text = "نص  اختباري فقط " * 80 + "\n\n" + "فقرة اختبار " * 30
    result = pages(text)
    for page in result:
        assert text[page["offset"] : page["offset"] + page["length"]] == page["text"]
    assert len(result) > 2


def test_audio_is_fully_decoded_and_duration_checked(tmp_path):
    path = tmp_path / "audio.mp3"
    # A synthetic tone is only a codec fixture; never a Quran source or preview.
    subprocess.run(  # noqa: S603 -- fixed FFmpeg fixture argv
        ["/usr/bin/ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", str(path)],
        check=True,
        capture_output=True,
    )
    assert 1 <= qf.audio_duration(path) <= 1.1
    path.write_bytes(path.read_bytes()[:10])
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        qf.audio_duration(path)


def test_production_requires_separate_approval_and_credentials_are_redacted(tmp_path):
    with pytest.raises(ValueError, match="PRODUCTION_NOT_APPROVED"):
        QuranPolicy(
            tmp_path / "unused.sock",
            environment="production",
            client_id="fixture-id",
            client_secret="fixture-secret",  # noqa: S106 -- fake credential  # noqa: S106 -- fake credential
        )
    policy = QuranPolicy(
        tmp_path / "unused.sock",
        client_id="fixture-id",
        client_secret="fixture-secret",  # noqa: S106 -- fake credential
    )
    assert "fixture-secret" not in repr(policy) and "fixture-id" not in repr(policy)


def test_oauth_token_cached_then_renewed_once_on_401(monkeypatch, tmp_path):
    egress = QuranEgress(
        QuranPolicy(
            tmp_path / "unused.sock",
            client_id="fixture-id",
            client_secret="fixture-secret",  # noqa: S106 -- fake credential
        )
    )
    calls = []

    def request(url, hosts, headers, **kwargs):
        calls.append((url, headers))
        if url.endswith("/oauth2/token"):
            return 200, "application/json", b'{"access_token":"fixture-token","expires_in":3600}'
        api_calls = [u for u, _ in calls if not u.endswith("/oauth2/token")]
        if len(api_calls) == 1:
            return 401, "application/json", b"{}"
        return 200, "application/json", b"{}"

    monkeypatch.setattr(egress, "_request", request)
    url = "https://apis-prelive.quran.foundation/content/api/v4/resources/tafsirs"
    egress.fetch(url)
    egress.fetch(url)
    assert len([u for u, _ in calls if u.endswith("/oauth2/token")]) == 2


def test_renderer_refuses_missing_network_sandbox_before_reading_content(monkeypatch):
    from studio_core.presentation import quran_clip

    def denied(_):
        raise RuntimeError("Renderer network isolation missing")

    monkeypatch.setattr(quran_clip, "proof", denied)
    with pytest.raises(RuntimeError, match="isolation missing"):
        quran_clip.render(Path("/does-not-exist"), APP)


def test_render_units_deny_internet_and_do_not_load_credentials():
    for bot in ("snap", "status"):
        unit = (REPO / f"config/systemd/madar-v3-{bot}-quran-render@.service").read_text()
        assert "PrivateNetwork=yes" in unit
        assert "RestrictAddressFamilies=AF_UNIX" in unit
        assert "InaccessiblePaths=-/run/madar -/etc/madar" in unit
        assert "EnvironmentFile" not in unit and "[Install]" not in unit
    assert not list((REPO / "config/systemd").glob("*quran*.timer"))


def test_kernel_denies_renderer_ip_sockets():
    worker = Path(__file__).with_name("quran_sandbox_worker.py")
    process = subprocess.run(  # noqa: S603 -- fixed test worker
        [sys.executable, str(worker), "proof"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(process.stdout) == {
        "ipv4": True,
        "ipv6": True,
        "udp": True,
        "egress_unreachable": True,
    }


@pytest.mark.parametrize("app,with_audio", [(APP, False), ("status-bot-v2", True)])
def test_real_clip_keeps_whole_audio_then_tafsir_inside_frame(tmp_path, catalog, app, with_audio):
    if shutil.which("pango-view") is None:
        pytest.skip("pango-view is required for real Quran clip render")
    audio = b"corrupt file"
    if with_audio:
        codec_fixture = tmp_path / "codec-fixture.mp3"
        subprocess.run(  # noqa: S603 -- codec fixture, not a Quran recording
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=duration=8",
                str(codec_fixture),
            ],
            check=True,
            capture_output=True,
        )
        audio = codec_fixture.read_bytes()
    _, path = fixture_bundle(tmp_path, catalog, app=app, client=FixtureClient(audio=audio))
    worker = Path(__file__).with_name("quran_sandbox_worker.py")
    process = subprocess.run(  # noqa: S603 -- fixed offline test worker
        [sys.executable, str(worker), str(path), app],
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert all(result["network_isolation"].values())
    assert result["audio_policy"] == ("HUMAN_RECITATION" if with_audio else "SILENT_AUDIO_FAILED")
    root = path.parent / "render"
    for name in ("ayah.png", "tafsir.png"):
        assert (root / name).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    measured = json.loads(
        subprocess.run(  # noqa: S603 -- fixed ffprobe fixture
            [
                "/usr/bin/ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                str(root / "video.mp4"),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert [s["codec_type"] for s in measured["streams"]] == (
        ["video", "audio"] if with_audio else ["video"]
    )
    assert (measured["streams"][0]["width"], measured["streams"][0]["height"]) == (1080, 1920)
    evidence = json.loads((root / "result.json").read_bytes())
    kinds = [scene["kind"] for scene in evidence["scenes"]]
    assert kinds[0] == "ayah" and kinds[-1] == "tafsir"
    assert evidence["text_sha256"] == qf.sha(qf.exact_ayah(2, 45).encode())
    assert evidence["duration"] >= evidence["recitation_seconds"]
    assert evidence["verse_seconds"] >= evidence["recitation_seconds"]


def test_application_adapters_bind_their_own_app_and_store(tmp_path, catalog, monkeypatch):
    from snap_bot.quran_clip import prepare_clip as snap_prepare
    from status_bot.quran_clip import prepare_clip as status_prepare
    from studio_core.religious import quran_commands

    monkeypatch.setattr(quran_commands, "NetworkClient", lambda _: FixtureClient())
    for adapter, app in ((snap_prepare, "snap-bot-v2"), (status_prepare, "status-bot-v2")):
        path = adapter(
            tmp_path / app, "a" * 32, 2, 45, socket=tmp_path / (app + ".sock"), catalog=catalog
        )
        value = qf.verify_bundle(path, app, time.time())
        assert value["app"] == app and value["audio_state"] == "SILENT_AUDIO_FAILED"
