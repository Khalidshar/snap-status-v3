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
    req = urllib.request.Request(srv.origin + path, data=body, headers=headers)  # noqa: S310
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
    body = b'{"app":"snap","command":"facts","request_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
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
