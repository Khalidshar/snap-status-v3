"""Private local v3 console. No external APIs, raw logs, secrets or schedulers."""

import argparse
import hmac
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

from studio_core.commands import COMMANDS
from studio_core.jobs.panel_queue import enqueue
from studio_core.sources import load as load_sources

REPO = Path(__file__).resolve().parents[1]
STATE = Path("/var/lib/madar")
UNITS = {"snap": "madar-v3-snap-commands.service", "status": "madar-v3-status-commands.service"}


def ctl(action: str, unit: str) -> bool:
    return (
        subprocess.run(  # noqa: S603
            ["/usr/bin/systemctl", action, unit],
            check=False,
            timeout=20,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def rows(path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[Any] | None:
    if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
        return None
    try:
        with sqlite3.connect("file:" + quote(str(path)) + "?mode=ro", uri=True, timeout=1) as db:
            db.execute("PRAGMA query_only=ON")
            return db.execute(sql, params).fetchall()
    except sqlite3.Error:
        return None


def snapshot(databases: dict[str, Path], binding: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "at": time.time(),
        "apps": {},
        "commands": [
            {"number": c.number, "key": c.key, "label": c.label, "confirm": c.confirm}
            for c in COMMANDS
        ],
    }
    for app, db in databases.items():
        try:
            active: bool | None = ctl("is-active", UNITS[app])
        except (OSError, subprocess.SubprocessError):
            active = None
        scope = "snap-bot-v2" if app == "snap" else "status-bot-v2"
        counts = rows(db, "SELECT state,count(*) FROM jobs WHERE app=? GROUP BY state", (scope,))
        outbox = (
            rows(
                db,
                "SELECT state,count(*) FROM whatsapp_outbox WHERE app=? GROUP BY state",
                (scope,),
            )
            if app == "status"
            else None
        )
        agents = rows(
            db,
            "SELECT role,status,started_at FROM agent_runs WHERE app=? ORDER BY started_at,run_id",
            (scope,),
        )
        # Export only known fields and enum values, never job/message/user payloads.
        allowed = {
            "QUEUED",
            "RUNNING",
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "EXPIRED",
            "RESEARCHING",
            "PROCESSING",
            "DOWNLOADING",
            "DESIGNING",
            "RENDERING",
            "VALIDATING",
            "REPAIRING",
            "DELIVERING",
            "ACK",
            "READY",
            "SENDING",
            "BLOCKED_OR_UNKNOWN",
            "READY_TO_PUBLISH",
            "PUBLISHED",
            "SEND_INTENT",
        }

        def aggregate(values: list[Any] | None, valid: set[str] = allowed) -> dict[str, int] | None:
            if values is None:
                return None
            totals: dict[str, int] = {}
            for key, count in values:
                key = key if key in valid else "OTHER"
                totals[key] = totals.get(key, 0) + count
            return totals

        inventory = json.loads((REPO / "config/status/agent-registry.json").read_text())["agents"]
        last = {r[0]: r[1] for r in agents or []}
        ledger = db.parent / "agent-status.sqlite"
        observed = {}
        if rows(ledger, "SELECT app FROM scope") == [(scope,)]:
            for role, state, at in rows(ledger, "SELECT role,state,at FROM runs ORDER BY id") or []:
                observed[role] = {"state": state, "at": at}
            last.update({role: row["state"] for role, row in observed.items()})

        item: dict[str, Any] = {
            "active": active,
            "jobs": aggregate(counts),
            "outbox": aggregate(outbox),
            "halted": (STATE / app / "HALT").exists(),
            "agents": [
                {
                    "name": a["name"],
                    "at": observed.get(a["id"], {}).get("at"),
                    "state": last.get(a["id"], "UNKNOWN")
                    if last.get(a["id"])
                    in {"PASS", "FAIL", "RUNNING", "STARTED", "SUCCEEDED", "FAILED"}
                    else "UNKNOWN",
                }
                for a in inventory
            ],
        }
        if app == "status":
            window = (
                rows(
                    db,
                    "SELECT last_inbound_at FROM whatsapp_windows WHERE identity_binding=?",
                    (binding,),
                )
                if binding
                else None
            )
            last_at = window[0][0] if window else None
            item["window"] = (
                "unknown"
                if last_at is None
                else "open"
                if isinstance(last_at, (int, float)) and 0 <= time.time() - last_at < 86400
                else "closed"
            )
            item["window_expires"] = last_at + 86400 if isinstance(last_at, (int, float)) else None
        # Show a safe, bounded event classification; never expose arbitrary log values.
        log = STATE / app / "events.jsonl"
        events = []
        clips = []
        errors = []
        if log.is_file() and not log.is_symlink():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 65536))
                raw = stream.read(65536)
            for line in raw.splitlines()[-20:]:
                try:
                    value = json.loads(line)
                    kind = value.get("kind")
                    sha = value.get("video_sha")
                    if (
                        isinstance(sha, str)
                        and len(sha) == 64
                        and all(c in "0123456789abcdef" for c in sha)
                    ):
                        clips.append(
                            {
                                "sha": sha,
                                "state": kind
                                if kind in {"ACK", "RENDERED", "APPROVED"}
                                else "UNKNOWN",
                            }
                        )
                    if kind in {"FAILED", "BLOCKED_OR_UNKNOWN", "SEND_FAILED"}:
                        errors.append("FAILED")
                    if kind in {"RENDERED", "ACK", "SENT", "APPROVED", "FAILED"}:
                        events.append(kind)
                except (ValueError, AttributeError):
                    continue
        item["events"] = events
        item["clips"] = clips[-5:]
        item["errors"] = errors
        item["sources"] = [
            {"name": source.name, "state": "excluded" if source.status == "excluded" else "unknown"}
            for source in load_sources()
            if source.kind == "rss"
            and any(k in source.commands for k in ("urgent", "news", "football"))
        ]
        result["apps"][app] = item
    usage = shutil.disk_usage(STATE if STATE.exists() else REPO)
    result["resources"] = {
        "disk_used": usage.used,
        "disk_total": usage.total,
        "load": os.getloadavg()[0],
        "cpus": os.cpu_count(),
    }
    return result


class Handler(BaseHTTPRequestHandler):
    server: Any

    def setup(self) -> None:
        self.request.settimeout(10)
        super().setup()

    def log_message(self, *args: Any) -> None:
        pass

    def reply(self, code: int, body: bytes, mime: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        return hmac.compare_digest(
            self.headers.get("Authorization", "").encode(), ("Bearer " + self.server.token).encode()
        )

    def valid_host(self) -> bool:
        return self.headers.get("Host") in {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }

    def do_GET(self) -> None:
        if not self.valid_host():
            self.reply(403, b"{}")
            return
        if self.path in {"/", "/app.js", "/style.css"}:
            name, mime = {
                "/": ("index.html", "text/html"),
                "/app.js": ("app.js", "text/javascript"),
                "/style.css": ("style.css", "text/css"),
            }[self.path]
            self.reply(200, (REPO / "apps/control-panel" / name).read_bytes(), mime)
        elif self.path == "/api/snapshot" and self.authorized():
            try:
                self.reply(
                    200,
                    json.dumps(
                        {
                            **snapshot(self.server.databases, self.server.binding),
                            "controls": self.server.controls,
                        }
                    ).encode(),
                )
            except (OSError, ValueError, subprocess.SubprocessError):
                self.reply(503, b"{}")
        else:
            self.reply(401, b"{}")

    def do_POST(self) -> None:
        if (
            not self.valid_host()
            or not self.authorized()
            or self.headers.get("Origin") != self.server.origin
        ):
            self.reply(403, b"{}")
            return
        if self.path != "/api/command" or not self.server.controls:
            self.reply(403, b"{}")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < length <= 512:
                raise ValueError("INVALID_REQUEST")
            value = json.loads(self.rfile.read(length))
            if set(value) != {"app", "command", "request_id"} or value["app"] not in UNITS:
                raise ValueError("INVALID_REQUEST")
            result = enqueue(
                STATE / value["app"] / "panel-commands.sqlite",
                value["command"],
                value["request_id"],
            )
            self.reply(202, json.dumps({"queued": result}).encode())
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
            self.reply(409, b'{"queued":false}')


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="لوحة تحكم v3 المحلية الخاصة")
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--snap-db", type=Path, default=STATE / "snap/jobs.sqlite")
    parser.add_argument("--status-db", type=Path, default=STATE / "status/commands/jobs.sqlite")
    parser.add_argument("--enable-controls", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("MADAR_PANEL_TOKEN", "")
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise SystemExit("يلزم رمز دخول خاص من ٣٢ محرفًا على الأقل في بيئة العملية")
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.token = token  # type: ignore[attr-defined]
        server.binding = os.environ.get("MADAR_PANEL_STATUS_BINDING", "")  # type: ignore[attr-defined]
        server.databases = {"snap": args.snap_db, "status": args.status_db}  # type: ignore[attr-defined]
        server.controls = args.enable_controls  # type: ignore[attr-defined]
        server.origin = f"http://127.0.0.1:{server.server_port}"  # type: ignore[attr-defined]
        server.control_lock = threading.Lock()  # type: ignore[attr-defined]
        server.serve_forever()


if __name__ == "__main__":
    main()
