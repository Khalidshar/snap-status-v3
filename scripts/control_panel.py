"""Private local v3 console. No external APIs, raw logs, secrets or schedulers."""

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import suppress
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

from studio_core.commands import COMMANDS
from studio_core.jobs import panel_schedule, panel_workspace
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
    try:
        if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
            return None
    except OSError:
        return None
    try:
        with sqlite3.connect("file:" + quote(str(path)) + "?mode=ro", uri=True, timeout=1) as db:
            db.execute("PRAGMA query_only=ON")
            return db.execute(sql, params).fetchall()
    except sqlite3.Error:
        return None


DESIGN_STYLES = {"footage", "mix", "solid"}
BACKGROUND_LICENSES = {
    "pexels": ("https://www.pexels.com/license/", "بيكسلز", "رخصة بيكسلز"),
    "pixabay": (
        "https://pixabay.com/service/license-summary/",
        "بيكساباي",
        "رخصة محتوى بيكساباي",
    ),
}


def background_labels(design: dict[str, Any]) -> tuple[str, str]:
    source = design.get("background_source")
    if isinstance(source, dict):
        provider = source.get("provider")
        known = BACKGROUND_LICENSES.get(provider) if isinstance(provider, str) else None
        if known:
            license_url, name, license_label = known
            return name, license_label if source.get(
                "license"
            ) == license_url else "رخصة غير معروفة"
    elif source is None and design.get("footage"):
        # The production renderer omits background_source when it uses the pinned Mixkit library.
        return "المكتبة المحلية", "رخصة ميكس كت المجانية للفيديو"
    return "مصدر غير معروف", "رخصة غير معروفة"


_designer_cache: dict[str, Any] = {"at": 0.0, "root": None, "value": []}


def designer_decisions(root: Path, limit: int = 12) -> list[dict[str, Any]]:
    """Latest decisions of the designer agent, read from finished render reports.

    Only the decision itself leaves: the look, backdrop title, dim, reasons and inspection.
    Re-scanned at most once a minute.
    """
    now = time.time()
    if _designer_cache["root"] == root and now - _designer_cache["at"] < 60:
        return list(_designer_cache["value"])
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        reports = sorted(
            root.glob("**/report.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:400]
    except OSError:
        reports = []
    for path in reports:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            design = value.get("design")
            if not isinstance(design, dict) or design.get("agent") != "designer":
                continue
            # A render keeps its report beside its attempt's copy: count each video once.
            video = str(value.get("video_sha256", path))
            if video in seen:
                continue
            seen.add(video)
            background_source, background_license = background_labels(design)
            found.append(
                {
                    "app": "snap" if value.get("app") == "snap-bot-v2" else "status",
                    "key": str(value.get("key", ""))[:32],
                    "style": design.get("style") if design.get("style") in DESIGN_STYLES else None,
                    "footage": str(design.get("footage_title", ""))[:80],
                    "background_source": background_source,
                    "background_license": background_license,
                    "dim": float(design.get("dim", 0)),
                    "reasons": [str(r)[:160] for r in design.get("reasons", [])][:5],
                    "bars": len((design.get("inspection") or {}).get("bars", [])),
                    "inspected": bool(design.get("inspection")),
                    "at": path.stat().st_mtime,
                }
            )
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if len(found) >= limit:
            break
    _designer_cache.update(at=now, root=root, value=found)
    return list(found)


STEP_STATES = {"RUNNING", "SUCCEEDED", "FAILED", "UNKNOWN"}
CLIP_ROOTS = {"snap": "snap/previews", "status": "status/content-clips"}


def _clip_title(folder: Path) -> str:
    for pattern in (
        "state/renders/*/input/content.json",
        "spool/renders/*/input/content.json",
        "spool/renders/*/attempts/*/input/content.json",
    ):
        for path in folder.glob(pattern):
            with suppress(OSError, ValueError, AttributeError):
                return str(json.loads(path.read_text(encoding="utf-8")).get("title", ""))[:40]
    return ""


def clip_trail(root: Path = STATE, limit: int = 8) -> list[dict[str, Any]]:
    try:
        return _clip_trail(root, limit)
    except (OSError, sqlite3.Error, ValueError):
        return []  # unreadable state never breaks the panel


def _clip_trail(root: Path, limit: int) -> list[dict[str, Any]]:
    """Each recent clip of both bots, agent by agent, read-only from its own job database.

    Owner decision 7 Nov 2026: see the agents work live. Only role names, states, times,
    durations and fixed failure codes leave; never message text, payloads or secrets.
    """
    commands = {
        r[0]: r[1]
        for r in rows(
            root / "status/commands/jobs.sqlite", "SELECT tag,text FROM whatsapp_commands"
        )
        or []
    }
    labels = {str(c.number): c.label for c in COMMANDS}
    clips: list[dict[str, Any]] = []
    for app, sub in CLIP_ROOTS.items():
        base = root / sub
        try:
            folders = sorted(
                (p for p in base.iterdir() if p.is_dir() and not p.is_symlink()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:limit]
        except OSError:
            continue
        for folder in folders:
            db = folder / "state/jobs.sqlite"
            timed = rows(
                db,
                "SELECT role,status,started_at,duration_ms,error_code FROM agent_runs"
                " ORDER BY started_at",
            )
            steps = [
                {"role": r, "state": s, "at": a, "ms": m, "code": c}
                for r, s, a, m, c in timed or []
                if s in STEP_STATES
            ]
            if app == "snap":
                # Snap keeps the twelve chain agents' outcomes in its contract table.
                seen = {s["role"] for s in steps}
                for role, state, code in (
                    rows(
                        db,
                        "SELECT role,status,error_code FROM owned_agent_contracts ORDER BY rowid",
                    )
                    or []
                ):
                    if state in STEP_STATES and role not in seen:
                        steps.append(
                            {"role": role, "state": state, "at": None, "ms": None, "code": code}
                        )
            text = str(commands.get(folder.name, ""))
            title = _clip_title(folder) or labels.get(text.removeprefix("c:"), text[:30])
            states = {s["state"] for s in steps}
            overall = (
                "FAILED"
                if "FAILED" in states
                else "RUNNING"
                if "RUNNING" in states or not steps
                else "SUCCEEDED"
            )
            if overall == "RUNNING" and time.time() - folder.stat().st_mtime > 1800:
                overall = "UNKNOWN"  # stopped without a final record
            clips.append(
                {
                    "app": app,
                    "id": folder.name[:12],
                    "title": title,
                    "at": folder.stat().st_mtime,
                    "state": overall,
                    "steps": steps[:24],
                }
            )
    clips.sort(key=lambda c: c["at"], reverse=True)
    return clips[: limit * 2]


def with_trail(state: dict[str, Any], trail: list[dict[str, Any]]) -> None:
    """Newer per-clip agent records win over the app ledger, which misses the six tools."""
    for app, item in state.get("apps", {}).items():
        latest: dict[str, tuple[str, float]] = {}
        for clip in trail:
            if clip["app"] != app:
                continue
            for step in clip["steps"]:
                at = step["at"] or clip["at"]
                if step["role"] not in latest or at > latest[step["role"]][1]:
                    latest[step["role"]] = (step["state"], at)
        for agent in item.get("agents", []):
            found = latest.get(agent["id"])
            if found and (agent.get("at") is None or found[1] > agent["at"]):
                agent["state"], agent["at"] = found


def source_health() -> dict[str, tuple[float, str]]:
    """Read operator-saved source-check output, emitting only a status and timestamp."""
    latest: dict[str, tuple[float, str]] = {}
    for app in UNITS:
        for command in COMMANDS:
            path = STATE / app / "source-checks" / f"{command.key}.json"
            if any(p.is_symlink() for p in (path, *path.parents)):
                continue
            try:
                with path.open("rb") as stream:
                    report = json.loads(stream.read(131072))
                for row in report["checks"]:
                    key = row["source_id"]
                    timestamp = datetime.fromisoformat(row["checked_at"]).timestamp()
                    if row["result"] in {"EXCLUDED", "NOT_A_FEED"}:
                        continue
                    state = "403" if row.get("http_status") == 403 else "down"
                    if row["result"] == "XML_VERIFIED":
                        state = "ok"
                    if key not in latest or timestamp > latest[key][0]:
                        latest[key] = (timestamp, state)
            except (OSError, ValueError, TypeError, KeyError):
                continue
    return latest


def snapshot(databases: dict[str, Path], binding: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "at": time.time(),
        "apps": {},
        "agent_roles": json.loads((REPO / "config/status/panel-agent-roles.json").read_text()),
        "commands": [
            {
                "number": c.number,
                "key": c.key,
                "label": c.label,
                "confirm": c.confirm,
                "section": c.section,
                "content": c.section in {"news", "general", "fashion", "faith", "brief"},
            }
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
        last_times = {r[0]: r[2] for r in agents or []}
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
                    "id": a["id"],
                    "name": a["name"],
                    "role": a["role"],
                    "at": observed.get(a["id"], {}).get("at", last_times.get(a["id"])),
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
        item["workspace"] = panel_workspace.view(STATE / app / "panel-commands.sqlite")
        result["apps"][app] = item
    usage = shutil.disk_usage(STATE if STATE.exists() else REPO)
    result["resources"] = {
        "disk_used": usage.used,
        "disk_total": usage.total,
        "load": os.getloadavg()[0],
        "cpus": os.cpu_count(),
    }
    checks = source_health()
    result["sources"] = [
        {
            "id": source.id,
            "name": source.name,
            "url": source.url,
            "state": source.status,
            "health": checks.get(source.id, (0, "unknown"))[1],
            "checked_at": checks.get(source.id, (0, "unknown"))[0],
        }
        for source in load_sources()
        if source.kind in {"rss", "website"}
    ]
    result["servers"] = {"amazon": None, "wsl": None}
    for host in result["servers"]:
        heartbeat = STATE / "panel" / f"{host}.json"
        if heartbeat.is_file() and not heartbeat.is_symlink():
            with suppress(OSError, ValueError):
                result["servers"][host] = json.loads(heartbeat.read_bytes())
    # The panel runs on the server it shows: read this machine directly instead of waiting
    # for an HTTPS heartbeat (owner, 7 Nov 2026: bots run on Khalid's WSL, Amazon stopped).
    host = local_host()
    current = result["servers"].get(host) if host else None
    fresh = isinstance(current, dict) and time.time() - float(current.get("at", 0)) < 90
    if host and not fresh:
        with suppress(OSError, ValueError, KeyError):
            result["servers"][host] = {**local_metrics(), "at": time.time(), "direct": True}
    result["running_on"] = host
    return result


def local_host() -> str | None:
    """'wsl' when this panel runs inside WSL, else None (Amazon reports by heartbeat)."""
    with suppress(OSError):
        if "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower():
            return "wsl"
    return None


def local_metrics() -> dict[str, Any]:
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in {"MemTotal", "MemAvailable"}:
            memory[key] = int(value.split()[0]) * 1024
    disk = shutil.disk_usage(STATE)
    units = {**UNITS, "snap_old": "snap-bot.timer", "status_old": "status-bot.timer"}
    return {
        "disk_used": disk.used,
        "disk_total": disk.total,
        "memory_total": memory["MemTotal"],
        "memory_available": memory["MemAvailable"],
        "load": os.getloadavg()[0],
        "services": {
            app: "active" if ctl("is-active", unit) else "inactive" for app, unit in units.items()
        },
    }


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
            "media-src 'self' blob:; img-src 'self' blob:; "
            "frame-ancestors 'none'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        return hmac.compare_digest(
            self.headers.get("Authorization", "").encode(), ("Bearer " + self.server.token).encode()
        )

    def heartbeat_authorized(self) -> bool:
        token = getattr(self.server, "reporter_token", "")
        return bool(
            token != self.server.token
            and len(token) >= 32
            and hmac.compare_digest(
                self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()
            )
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
        if self.path in {"/", "/app.js", "/style.css", "/display.woff"}:
            name, mime = {
                "/": ("index.html", "text/html"),
                "/app.js": ("app.js", "text/javascript"),
                "/style.css": ("style.css", "text/css"),
                "/display.woff": ("display.woff", "font/woff"),
            }[self.path]
            asset = (
                REPO / "assets/fonts/readexpro/ReadexPro.woff"
                if name == "display.woff"
                else REPO / "scripts/control_panel_ui" / name
            )
            self.reply(200, asset.read_bytes(), mime)
        elif self.path.startswith("/api/media/") and self.authorized():
            match = re.fullmatch(r"/api/media/(snap|status)/([0-9a-f]{32})", self.path)
            if not match:
                self.reply(404, b"{}")
                return
            app, identifier = match.groups()
            path = STATE / app / "panel-media" / (identifier + ".mp4")
            try:
                if any(p.is_symlink() for p in (path, *path.parents)):
                    raise ValueError("UNSAFE_MEDIA")
                raw = path.read_bytes()
                previews = panel_workspace.view(STATE / app / "panel-commands.sqlite")["previews"]
                if not any(
                    p["id"] == identifier and p["sha"] == hashlib.sha256(raw).hexdigest()
                    for p in previews
                ):
                    raise ValueError("UNBOUND_MEDIA")
                self.reply(200, raw, "video/mp4")
            except (OSError, ValueError):
                self.reply(404, b"{}")
        elif self.path == "/api/snapshot" and self.authorized():
            try:
                state = snapshot(self.server.databases, self.server.binding)
                trail = clip_trail()
                with_trail(state, trail)
                self.reply(
                    200,
                    json.dumps(
                        {
                            **state,
                            "designer": designer_decisions(self.server.designer_root),
                            "trail": trail,
                            "controls": self.server.controls,
                            "workspace_executor_connected": True,
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
            or not (
                self.heartbeat_authorized() if self.path == "/api/heartbeat" else self.authorized()
            )
            or (self.path != "/api/heartbeat" and self.headers.get("Origin") != self.server.origin)
        ):
            self.reply(403, b"{}")
            return
        if self.path == "/api/heartbeat":
            self.heartbeat()
            return
        if self.path == "/api/upload" and self.server.controls:
            self.upload()
            return
        if self.path == "/api/action" and self.server.controls:
            try:
                value = self.read_json(4096)
                if (
                    set(value) != {"app", "action", "payload", "request_id"}
                    or value["app"] not in UNITS
                ):
                    raise ValueError("INVALID_ACTION")
                queued = panel_workspace.submit(
                    STATE / value["app"] / "panel-commands.sqlite",
                    value["request_id"],
                    value["action"],
                    value["payload"],
                )
                self.reply(202, json.dumps({"queued": queued}).encode())
            except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
                self.reply(409, b'{"queued":false}')
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
            if value["command"] not in {
                "halt",
                "unhalt",
                "status",
                "agents",
                "report",
                "schedule",
                "menu",
            }:
                raise ValueError("USE_REVIEW_WORKSPACE")
            result = enqueue(
                STATE / value["app"] / "panel-commands.sqlite",
                value["command"],
                value["request_id"],
            )
            self.reply(202, json.dumps({"queued": result}).encode())
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
            self.reply(409, b'{"queued":false}')

    def read_json(self, maximum: int) -> Any:
        length = int(self.headers.get("Content-Length", "0"))
        if self.headers.get("Transfer-Encoding") or not 0 < length <= maximum:
            raise ValueError("INVALID_LENGTH")
        return json.loads(self.rfile.read(length))

    def heartbeat(self) -> None:
        # Separate credential: the reporter cannot operate owner controls.
        try:
            value = self.read_json(4096)
            if not isinstance(value, dict):
                raise ValueError("INVALID_HEARTBEAT")
            host = value.pop("host", "wsl")
            if host not in {"amazon", "wsl"}:
                raise ValueError("INVALID_HOST")
            schedules = value.pop("schedules", {})
            panel_schedule.validate(schedules)
            if set(value) != {
                "disk_used",
                "disk_total",
                "memory_total",
                "memory_available",
                "load",
                "services",
            }:
                raise ValueError("INVALID_HEARTBEAT")
            for key in set(value) - {"services"}:
                if type(value[key]) not in (int, float) or not 0 <= value[key] <= 10**16:
                    raise ValueError("INVALID_METRIC")
            if not isinstance(value["services"], dict) or set(value["services"]) - {
                "snap",
                "status",
                "snap_old",
                "status_old",
            }:
                raise ValueError("INVALID_SERVICES")
            if any(v not in {"active", "inactive", "unknown"} for v in value["services"].values()):
                raise ValueError("INVALID_SERVICE_STATE")
            target = STATE / "panel"
            target.mkdir(parents=True, mode=0o700, exist_ok=True)
            if target.is_symlink() or (target / f"{host}.json").is_symlink():
                raise ValueError("UNSAFE_HEARTBEAT")
            temporary = target / (uuid.uuid4().hex + ".json")
            temporary.write_text(json.dumps({**value, "schedules": schedules, "at": time.time()}))
            temporary.chmod(0o600)
            temporary.replace(target / f"{host}.json")
            self.reply(202, b"{}")
        except (ValueError, TypeError, OSError):
            self.reply(400, b"{}")

    def upload(self) -> None:
        try:
            app = self.headers.get("X-Panel-App")
            kind = self.headers.get("X-Panel-Kind")
            mime = self.headers.get("Content-Type")
            length = int(self.headers.get("Content-Length", "0"))
            if (
                app not in UNITS
                or kind not in {"upload", "statistics"}
                or self.headers.get("Transfer-Encoding")
                or not 0 < length <= 50_000_000
                or mime
                not in (
                    {"video/mp4", "video/quicktime"}
                    if kind == "upload"
                    else {"image/png", "image/jpeg"}
                )
            ):
                raise ValueError("INVALID_UPLOAD")
            raw = self.rfile.read(length)
            if len(raw) != length or not (
                (kind == "upload" and raw[4:8] == b"ftyp")
                or (
                    kind == "statistics"
                    and (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff"))
                )
            ):
                raise ValueError("INVALID_MEDIA")
            identifier = uuid.uuid4().hex
            directory = STATE / app / "panel-uploads"
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            if any(p.is_symlink() for p in (directory, *directory.parents)):
                raise ValueError("UNSAFE_UPLOAD")
            with (directory / identifier).open("xb") as stream:
                stream.write(raw)
            panel_workspace.submit(
                STATE / app / "panel-commands.sqlite", identifier, kind, {"asset": identifier}
            )
            self.reply(202, json.dumps({"id": identifier, "queued": True}).encode())
        except (ValueError, TypeError, OSError, sqlite3.Error):
            self.reply(400, b"{}")


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="لوحة تحكم v3 المحلية الخاصة")
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--snap-db", type=Path, default=STATE / "snap/jobs.sqlite")
    parser.add_argument("--status-db", type=Path, default=STATE / "status/commands/jobs.sqlite")
    parser.add_argument("--enable-controls", action="store_true")
    # Where finished render reports live (their "design" block is the designer's decision).
    parser.add_argument("--designer-root", type=Path, default=STATE)
    args = parser.parse_args()
    token = os.environ.get("MADAR_PANEL_TOKEN", "")
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise SystemExit("يلزم رمز دخول خاص من ٣٢ محرفًا على الأقل في بيئة العملية")
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.reporter_token = os.environ.get("MADAR_REPORTER_TOKEN", "")
        server.token = token  # type: ignore[attr-defined]
        server.binding = os.environ.get("MADAR_PANEL_STATUS_BINDING", "")  # type: ignore[attr-defined]
        server.databases = {"snap": args.snap_db, "status": args.status_db}  # type: ignore[attr-defined]
        server.controls = args.enable_controls  # type: ignore[attr-defined]
        server.designer_root = args.designer_root  # type: ignore[attr-defined]
        server.origin = f"http://127.0.0.1:{server.server_port}"  # type: ignore[attr-defined]
        server.control_lock = threading.Lock()  # type: ignore[attr-defined]
        server.serve_forever()


if __name__ == "__main__":
    main()
