"""Offline coverage for the QF render wrapper, owned adoption and preview publication."""

import hashlib
import importlib
import json
import sqlite3
import struct
import subprocess
import time
import tomllib
from http.client import HTTPResponse
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from snap_bot import actions_preview, service
from studio_core.config import Config
from studio_core.presentation.brand_frame import load_brand
from studio_core.production import pipeline
from studio_core.production.model import canonical, phrases_for, scene, timed
from studio_core.religious import quran_foundation, quran_pipeline
from studio_core.weather import content_hash


class WorkerConnection:
    """Run the real handler and HTTP serialization without a host socket."""

    def __init__(self, server):
        self.server = server
        self.sock = self

    def getsockopt(self, *args):
        return struct.pack("iII", 0, self.server.settings["renderer_uid"], 0)

    def connect(self):
        pass

    def close(self):
        pass

    def request(self, method, path, body):
        payload = body.encode()
        self.request_bytes = (
            f"{method} {path} HTTP/1.0\r\nContent-Length: {len(payload)}\r\n\r\n".encode() + payload
        )
        self.reply = bytearray()
        service.RenderHandler(self, ("local", 0), self.server)

    def makefile(self, *args):
        return BytesIO(self.request_bytes)

    def sendall(self, data):
        self.reply.extend(data)

    def getresponse(self):
        response = HTTPResponse(SimpleNamespace(makefile=lambda *args: BytesIO(self.reply)))
        response.begin()
        return response


def test_qf_tilawa_crosses_worker_and_preview_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("MADAR_LEGACY_PRODUCTION", "1")
    monkeypatch.setenv("MADAR_QURAN_AUDIO", "1")
    monkeypatch.setenv("MADAR_PRODUCTION_STATE_ROOT", str(tmp_path / "snap/production"))
    # The worker is simulated, so this fixture's private pytest directory is shared.
    monkeypatch.setattr(actions_preview, "renderer_workspace_parent", lambda: tmp_path)
    store = quran_foundation.Store(tmp_path / "quran-snap", "snap-bot-v2")
    monkeypatch.setattr(quran_pipeline, "store_for", lambda app: store)
    qf_job = "a" * 32
    _, expires = store.reserve(qf_job, {"reciters": [{"id": 999, "name": "قارئ"}]})
    # A restricted renderer may register copies but must not sweep coordinator files.
    monkeypatch.setattr(store, "sweep", lambda: pytest.fail("sweep in render wrapper"))
    monkeypatch.setattr(pipeline.footage, "prepare", lambda *args, **kwargs: None)
    audio = b"offline recording fixture"
    filename = "assets/000.mp3"
    video = b"\0\0\0\x08ftyp\0\0\0\x08moov\0\0\0\x08mdat"
    video_sha = hashlib.sha256(video).hexdigest()

    def prepare(app, kind, job, root):
        assert kind == "verse"
        ayah = quran_foundation.exact_ayah(94, 5)
        words = ayah.split()[:-1]
        phrases = phrases_for(
            ayah, [[i, i + 1, i * 300, (i + 1) * 300] for i in range(len(words))], 3
        )
        tafsir = "نص وصفي للاختبار " * 3000  # exercise the worker reply above the old 64 KiB cap
        tafsir_bytes = canonical({"result": {"sura": 94, "aya": 5, "translation": tafsir}})
        scenes, seconds = timed(
            [
                scene("آية اليوم", 3, role="hook"),
                scene(
                    ayah,
                    58,  # 3 + 58 + 5 = 66 s, over the 65 s Snap floor (10 Nov)
                    role="verse",
                    verse=[94, 5],
                    phrases=phrases,
                    source="مؤسسة القرآن",
                    audio=filename,
                    audio_duration=3,
                    tafsir=tafsir,
                    tafsir_sha256=hashlib.sha256(tafsir.encode()).hexdigest(),
                ),
                scene("تابعني للمزيد", 5, role="cta"),
            ]
        )
        plan = {
            "schema": 1,
            "app": app,
            "key": kind,
            "scenes": scenes,
            "duration": seconds,
            "music": None,
            "effects": False,
            "sources": [],
            "assets": [
                {
                    "file": filename,
                    "sha256": hashlib.sha256(audio).hexdigest(),
                    "source_url": "quran-foundation:999/94/5",
                    "qf_job": qf_job,
                    "expires_at": expires,
                },
                {
                    "file": "assets/001.json",
                    "sha256": hashlib.sha256(tafsir_bytes).hexdigest(),
                    "source_url": "https://quranenc.com/api/v1/translation/aya/arabic_moyassar/94/5",
                },
            ],
            "audio": [{"file": filename, "at": 3.15, "seconds": 3, "kind": "human"}],
        }
        content = {
            "type": "did_you_know",
            "kind": kind,
            "sacred": True,
            "language": "ar",
            "title": "آية اليوم",
            "body": "\n".join(item["body"] for item in scenes),
            "fact_id": "legacy-" + hashlib.sha256(canonical(plan)).hexdigest()[:32],
            "category": kind,
            "source": "legacy-production",
            "source_display": "مصدر تجريبي",
            "source_url": "",
            "verified_at": "2026-11-08",
            "attribution": "روّق مع خالد",
            "footer": "تابعني للمزيد",
            "fetched_at": "2026-11-08T00:00:00Z",
            "expires_at": time.time() + 300,
            "legacy_production": plan,
        }
        content["content_hash"] = content_hash(content)
        (root / filename).parent.mkdir(parents=True)
        (root / filename).write_bytes(audio)
        (root / "assets/001.json").write_bytes(tafsir_bytes)
        (root / "content.json").write_bytes(canonical(content))
        return content

    def render_scenes(source, work, *, history):
        plan = json.loads((source / "content.json").read_bytes())["legacy_production"]
        assert plan["scenes"][1]["phrases"] and (source / filename).read_bytes() == audio
        attempt = work / "attempt-1"
        attempt.mkdir(parents=True)
        (attempt / "montage.mp4").write_bytes(b"silent backdrop")
        (attempt / "clip.mp4").write_bytes(video)
        (attempt / "mix.wav").write_bytes(audio)
        (work / "video.mp4").write_bytes(video)
        # FFmpeg decodes the small PPM fixture by its contents to make the real PNG preview.
        (work / "shot-3.jpg").write_bytes(b"P6\n2 2\n255\n" + b"\x00" * 12)
        brand = load_brand()
        return {
            "brand": {"clip_box": brand["clip_box"], "overlay_sha256": brand["overlay"]["sha256"]},
            "attempt": 1,
            "network_isolation": dict.fromkeys(("ipv4", "ipv6", "udp", "egress_unreachable"), True),
            "qa": {
                "result": "PASS",
                "audio": "HUMAN_RECORDING",
                "width": 1080,
                "height": 1920,
                "codec": "h264",
                "duration": plan["duration"],
                "bytes": len(video),
                "audio_qa": {"integrated_lufs": -16, "true_peak_dbtp": -2},
                "judge": {
                    "decision": "PASS",
                    "score": 10,
                    "shots": [1, 2, 3, 4, 5, 6],
                    "criteria": dict.fromkeys(
                        (
                            "dimensions",
                            "codec",
                            "duration",
                            "size",
                            "audio",
                            "safe_window",
                            "content",
                            "handle",
                            "follow_card",
                            "source_images",
                        ),
                        True,
                    ),
                },
            },
        }

    monkeypatch.setattr(pipeline, "prepare", prepare)
    monkeypatch.setattr(
        importlib.import_module("studio_core.production.render"), "render", render_scenes
    )
    real_run = subprocess.run

    def launch(argv, **kwargs):
        if argv[:3] != ["sudo", "-n", "systemd-run"]:
            return real_run(argv, **kwargs)
        assert "--property=ProtectSystem=strict" in argv
        assert "--property=PrivateTmp=yes" in argv
        assert "--property=RestrictAddressFamilies=AF_UNIX" in argv
        config = Path(argv[argv.index("--config") + 1])
        settings = tomllib.loads(config.read_text())["slice"]
        Path(settings["render_socket"]).touch()
        server = SimpleNamespace(config=Config.load(config), settings=settings)
        monkeypatch.setattr(service, "UnixHTTPConnection", lambda *args: WorkerConnection(server))

    monkeypatch.setattr(actions_preview.subprocess, "run", launch)
    output = tmp_path / "snap" / "bundle"
    result = actions_preview.render_bundle(
        output, {"kind": "verse", "run_id": "1", "head_sha": "b" * 40}
    )

    assert result["delivery"] == "NOT_ATTEMPTED"
    assert result["quality"]["contracts"]["quality"]
    assert result["render"]["video_sha256"] == video_sha
    assert (output / "did-you-know.mp4").read_bytes() == video
    assert (output / "preview.png").is_file()
    with sqlite3.connect(store.db) as db:
        copies = dict(db.execute("SELECT path,sha FROM copies WHERE job=?", (qf_job,)))
    assert copies[str(output / "did-you-know.mp4")] == video_sha
    assert copies[str(output / "preview.png")] == result["preview_sha256"]
    assert any(path.endswith("/attempt-1/clip.mp4") for path in copies)
    assert not any(path.endswith("/montage.mp4") for path in copies)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ValueError("QF_COPY_TYPE_DENIED"), "QF_COPY_TYPE_DENIED"),
        (
            ValueError("LEGACY_QA_FAILED_AFTER_REBUILD: private detail"),
            "LEGACY_QA_FAILED_AFTER_REBUILD",
        ),
        (ValueError("private path /example"), None),
        (OSError("private path /example"), None),
    ],
)
def test_renderer_reports_only_safe_failure_codes(monkeypatch, capsys, error, code):
    def fail(*args):
        raise error

    monkeypatch.setattr(service, "render", fail)
    server = SimpleNamespace(config=None, settings={"worker_uid": 1, "renderer_uid": 1})
    worker = WorkerConnection(server)
    worker.request(
        "POST",
        "/render",
        json.dumps({"job_id": "a" * 32, "content_hash": "b" * 64, "attempt": 1}),
    )
    response = worker.getresponse()
    expected = {"error": type(error).__name__}
    if code:
        expected["code"] = code
    assert response.status == 422
    assert json.loads(response.read()) == expected
    assert capsys.readouterr().err.strip() == " ".join(["SNAP_RENDER_FAILED", *expected.values()])
