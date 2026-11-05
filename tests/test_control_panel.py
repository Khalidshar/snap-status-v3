import importlib.util
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location(
    "control_panel", Path(__file__).parents[1] / "scripts/control_panel.py"
)
panel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(panel)


@pytest.fixture
def server(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
    srv.token = "fixture-" * 5
    srv.binding = "owner"
    srv.databases = {"snap": tmp_path / "absent", "status": tmp_path / "status.sqlite"}
    srv.designer_root = tmp_path
    srv.controls = False
    srv.origin = f"http://127.0.0.1:{srv.server_port}"
    srv.control_lock = threading.Lock()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join()


def get(srv, path, auth=True, body=None, origin=None):
    headers = {"Authorization": "Bearer " + srv.token} if auth else {}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(  # noqa: S310 - test server on loopback
        srv.origin + path, data=body, headers=headers
    )
    return urllib.request.urlopen(req, timeout=3)  # noqa: S310


def test_auth_and_static(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server, "/api/snapshot", auth=False)
    assert exc.value.code == 401
    with get(server, "/", auth=False) as reply:
        assert b'dir="rtl"' in reply.read()
        assert "frame-ancestors 'none'" in reply.headers["Content-Security-Policy"]


def test_missing_is_unknown_and_window_owner_scope(server):
    db = server.databases["status"]
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE whatsapp_windows(identity_binding TEXT, last_inbound_at REAL)")
        conn.execute("INSERT INTO whatsapp_windows VALUES ('foreign',?)", (panel.time.time(),))
    with patch.object(panel, "ctl", return_value=False), get(server, "/api/snapshot") as reply:
        data = json.loads(reply.read())
    assert data["apps"]["snap"]["jobs"] is None
    assert data["apps"]["status"]["window"] == "unknown"
    assert not server.databases["snap"].exists()
    assert "fixture" not in json.dumps(data)


def test_control_defaults_and_csrf(server):
    data = b'{"app":"snap","command":"facts","request_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
    with patch.object(panel, "ctl") as ctl:
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(server, "/api/command", body=data, origin=server.origin)
        assert exc.value.code == 403
        server.controls = True
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(server, "/api/command", body=data, origin="https://foreign.example")
        assert exc.value.code == 403
        ctl.assert_not_called()


def test_command_queue_idempotency(server, tmp_path):
    server.controls = True
    body = b'{"app":"snap","command":"status","request_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
    with patch.object(panel, "STATE", tmp_path), patch.object(panel, "ctl") as ctl:
        with get(server, "/api/command", body=body, origin=server.origin) as reply:
            assert json.loads(reply.read())["queued"]
        with get(server, "/api/command", body=body, origin=server.origin) as reply:
            assert not json.loads(reply.read())["queued"]
        ctl.assert_not_called()
    with sqlite3.connect(tmp_path / "snap/panel-commands.sqlite") as db:
        assert db.execute("SELECT count(*) FROM panel_commands").fetchone()[0] == 1


def test_command_unknown_not_replayed(tmp_path):
    from studio_core.jobs import panel_queue

    path = tmp_path / "queue.sqlite"
    panel_queue.enqueue(path, "facts", "a" * 32)
    item = panel_queue.claim(path)
    assert item["command"] == "facts"
    assert panel_queue.claim(path) is None
    panel_queue.finish(path, "a" * 32, False)
    assert panel_queue.claim(path) is None


def test_legacy_content_cannot_bypass_review(server, tmp_path):
    server.controls = True
    for command in ("facts", "verse", "send", "resume"):
        body = json.dumps({"app": "snap", "command": command, "request_id": "a" * 32}).encode()
        with patch.object(panel, "STATE", tmp_path), pytest.raises(urllib.error.HTTPError) as exc:
            get(server, "/api/command", body=body, origin=server.origin)
        assert exc.value.code == 409
    assert not (tmp_path / "snap/panel-commands.sqlite").exists()


def test_action_reservation_is_not_a_platform_call(server, tmp_path):
    server.controls = True
    body = json.dumps(
        {
            "app": "status",
            "action": "produce",
            "payload": {"command": "verse"},
            "request_id": "a" * 32,
        }
    ).encode()
    with patch.object(panel, "STATE", tmp_path), patch.object(panel, "ctl") as ctl:
        with get(server, "/api/action", body=body, origin=server.origin) as reply:
            assert json.loads(reply.read())["queued"]
        ctl.assert_not_called()
    workspace = panel.panel_workspace.view(tmp_path / "status/panel-commands.sqlite")
    assert workspace["actions"][0]["state"] == "QUEUED"
    assert workspace["previews"] == []


@pytest.mark.parametrize("host", (None, "amazon", "wsl"))
def test_heartbeat_token_cannot_control_owner_panel(server, tmp_path, host):
    server.reporter_token = "reporter-" * 8
    metric = {
        "disk_used": 10,
        "disk_total": 100,
        "memory_total": 100,
        "memory_available": 50,
        "load": 0.1,
        "services": {"snap": "inactive"},
    }
    if host is not None:
        metric["host"] = host
        metric["schedules"] = {
            "snap": {"paused": False, "slots": [["tech", "10:00"], ["tech", "20:00"]]}
        }
    headers = {"Authorization": "Bearer " + server.reporter_token}
    with patch.object(panel, "STATE", tmp_path):
        req = urllib.request.Request(  # noqa: S310 - test server on loopback
            server.origin + "/api/heartbeat", data=json.dumps(metric).encode(), headers=headers
        )
        with urllib.request.urlopen(req, timeout=3) as response:  # noqa: S310
            assert response.status == 202
        assert (
            json.loads((tmp_path / "panel" / f"{host or 'wsl'}.json").read_text())["services"][
                "snap"
            ]
            == "inactive"
        )
        with get(server, "/api/snapshot") as snapshot_response:
            snapshot = json.load(snapshot_response)
        assert snapshot["servers"][host or "wsl"]["schedules"] == metric.get("schedules", {})
        req = urllib.request.Request(  # noqa: S310 - test server on loopback
            server.origin + "/api/snapshot", headers=headers
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3)  # noqa: S310
        assert exc.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(server, "/api/heartbeat", body=json.dumps(metric).encode(), origin=server.origin)
        assert exc.value.code == 403


def test_upload_validates_signature_and_queues_only(server, tmp_path):
    server.controls = True
    headers = {
        "Authorization": "Bearer " + server.token,
        "Origin": server.origin,
        "Content-Type": "video/mp4",
        "X-Panel-App": "snap",
        "X-Panel-Kind": "upload",
    }
    with patch.object(panel, "STATE", tmp_path), patch.object(panel, "ctl") as ctl:
        bad = urllib.request.Request(  # noqa: S310 - test server on loopback
            server.origin + "/api/upload", data=b"<html>bad</html>", headers=headers
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(bad, timeout=3)  # noqa: S310
        assert exc.value.code == 400
        raw = b"\x00\x00\x00\x20ftyp" + b"fixture"
        good = urllib.request.Request(  # noqa: S310 - test server on loopback
            server.origin + "/api/upload", data=raw, headers=headers
        )
        with urllib.request.urlopen(good, timeout=3) as response:  # noqa: S310
            identifier = json.loads(response.read())["id"]
        ctl.assert_not_called()
    assert (tmp_path / "snap/panel-uploads" / identifier).read_bytes() == raw
    assert (
        panel.panel_workspace.view(tmp_path / "snap/panel-commands.sqlite")["actions"][0]["action"]
        == "upload"
    )


def test_media_preview_digest_and_path_admission(server, tmp_path):
    path = tmp_path / "snap/panel-commands.sqlite"
    panel.panel_workspace.record_preview(path, "a" * 32, "verse", b"video", [])
    with patch.object(panel, "STATE", tmp_path):
        with get(server, "/api/media/snap/" + "a" * 32) as response:
            assert response.read() == b"video"
        (tmp_path / "snap/panel-media" / ("a" * 32 + ".mp4")).write_bytes(b"tampered")
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(server, "/api/media/snap/" + "a" * 32)
        assert exc.value.code == 404
        with pytest.raises(urllib.error.HTTPError):
            get(server, "/api/media/snap/../../etc/passwd")


def test_development_metrics_are_not_amazon_and_roles_are_exact(server):
    with (
        patch.dict(panel.os.environ, {"MADAR_PANEL_HOST": "development"}),
        patch.object(panel, "ctl", return_value=None),
    ):
        result = panel.snapshot(server.databases, "")
    assert result["servers"]["amazon"] is None
    roles = result["agent_roles"]
    assert len(roles) == 18
    assert [a["step"] for a in roles if a["chain"]] == list(range(1, 13))
    assert len([a for a in roles if not a["chain"]]) == 6
    for app in result["apps"].values():
        assert {a["id"] for a in app["agents"]} == {a["id"] for a in roles}
        assert all(a["state"] == "UNKNOWN" for a in app["agents"])


def test_designer_decisions_export_only_the_decision(tmp_path):
    work = tmp_path / "snap/spool/job/attempts/1/production"
    work.mkdir(parents=True)
    (work / "report.json").write_text(
        json.dumps(
            {
                "app": "snap-bot-v2",
                "key": "news",
                "content_hash": "secret-payload-hash",
                "design": {
                    "agent": "designer",
                    "style": "solid",
                    "footage_title": "Sun over hills",
                    "background_source": {
                        "provider": "pexels",
                        "license": "https://www.pexels.com/license/",
                        "source": "secret-payload-url",
                    },
                    "footage_path": "/opt/private/path.mp4",
                    "dim": 0.41,
                    "reasons": ["كل البطاقات فيها صور"],
                    "inspection": {"moments": 5, "bars": []},
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "other.json").write_text("{}")
    panel._designer_cache.update(at=0.0, root=None, value=[])
    [decision] = panel.designer_decisions(tmp_path)
    assert decision["app"] == "snap" and decision["style"] == "solid" and decision["bars"] == 0
    assert decision["inspected"] and decision["reasons"] == ["كل البطاقات فيها صور"]
    assert decision["background_source"] == "بيكسلز"
    assert decision["background_license"] == "رخصة بيكسلز"
    assert "footage_path" not in json.dumps(decision) and "secret" not in json.dumps(decision)


def test_background_labels_for_library_and_pixabay():
    assert panel.background_labels({"footage": "local.mp4"}) == (
        "المكتبة المحلية",
        "رخصة ميكس كت المجانية للفيديو",
    )
    assert panel.background_labels(
        {
            "background_source": {
                "provider": "pixabay",
                "license": "https://pixabay.com/service/license-summary/",
            }
        }
    ) == ("بيكساباي", "رخصة محتوى بيكساباي")
    assert panel.background_labels({"background_source": {"provider": "pexels"}}) == (
        "بيكسلز",
        "رخصة غير معروفة",
    )
