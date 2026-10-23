"""Telegram egress: fixed owner, normalized inputs, bounded Unix RPC, no generic URLs."""

import argparse
import base64
import email.policy
import json
import os
import re
import socketserver
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from studio_core.network import UnixHTTPConnection

from .telegram_egress import EXPECTED_BOT, api, multipart

COMMANDS = {"start", "help", "weather", "status"}
MESSAGES = {
    "ready": "بوت Snap V2 جاهز. أرسل /weather لفيديو طقس الرياض، و/status لحالة التشغيل.",
    "help": "الأوامر: /weather طقس الرياض — /status حالة التشغيل — /help المساعدة. البوت التجريبي مخصص للمالك.",
    "queued": "بدأ تجهيز فيديو طقس الرياض.",
    "busy": "توجد مهمة نشطة أو وصلت إلى الحد المؤقت. حاول بعد قليل.",
    "unchanged": "بيانات الطقس لم تتغير؛ الفيديو الحالي موجود بالفعل في هذه المحادثة.",
    "failed": "تعذر إكمال المهمة. حُفظ الخطأ للفحص، ولن يُعاد إرسال نتيجة غير مؤكدة تلقائيًا.",
    "status": "المستمع يعمل، والأمر /weather متاح. النسخة الحالية تجريبية للطقس.",
}


def normalize_updates(updates: Any, owner: str, now: float) -> dict[str, Any]:
    accepted: list[dict[str, Any]] = []
    cursor = 0
    for update in updates:
        uid = update.get("update_id")
        if type(uid) is not int or uid < 0:
            continue
        cursor = max(cursor, uid + 1)
        message = update.get("message", {})
        chat = message.get("chat", {})
        actor = message.get("from", {})
        if (
            chat.get("type") != "private"
            or str(chat.get("id")) != owner
            or str(actor.get("id")) != owner
            or actor.get("is_bot")
        ):
            continue
        at = message.get("date")
        text = message.get("text", "")
        if type(at) is not int or not 0 <= now - at <= 900 or not isinstance(text, str):
            continue
        match = re.fullmatch(
            r"/([a-z]+)(?:@" + EXPECTED_BOT + r")?", text.strip(), flags=re.IGNORECASE
        )
        if match and match[1].lower() in COMMANDS:
            accepted.append(
                {"update_id": uid, "actor": owner, "command": match[1].lower(), "date": at}
            )
    return {"offset": cursor, "commands": accepted}


def validated_video(body: bytes, kind: str, owner: str) -> tuple[bytes, str]:
    if not kind.startswith("multipart/form-data; boundary="):
        raise ValueError("Invalid multipart")
    boundary = kind.split("boundary=", 1)[1]
    if not re.fullmatch("studio[a-f0-9]{32}", boundary):
        raise ValueError("Invalid boundary")
    mail: Any = BytesParser(policy=email.policy.default).parsebytes(
        ("Content-Type: " + kind + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body
    )
    parts = list(mail.iter_parts())
    names = [p.get_param("name", header="content-disposition") for p in parts]
    if sorted(names) != sorted(["chat_id", "caption", "supports_streaming", "video"]):
        raise ValueError("Unexpected fields")
    fields = dict(zip(names, parts, strict=True))
    if fields["chat_id"].get_payload(decode=True).decode() != owner:
        raise ValueError("Wrong owner")
    video = fields["video"].get_payload(decode=True)
    if fields["video"].get_content_type() != "video/mp4" or not 0 < len(video) <= 2_000_000:
        raise ValueError("Invalid video")
    # Rebuild every field: caller cannot add a destination, caption, or arbitrary method.
    return multipart(owner, video)


class Client:
    def __init__(self, path: str) -> None:
        self.path = path

    def call(self, path: str, value: Any) -> Any:
        connection = UnixHTTPConnection(self.path, 60)
        try:
            connection.request(
                "POST", path, json.dumps(value).encode(), {"Content-Type": "application/json"}
            )
            response = connection.getresponse()
            raw = response.read(3_000_001)
            if len(raw) > 3_000_000 or response.status != 200:
                raise RuntimeError("Telegram egress RPC failed")
            return json.loads(raw)
        finally:
            connection.close()

    def telegram(
        self, ignored_token: object, method: str, body: bytes = b"", kind: str = "application/json"
    ) -> Any:
        return self.call(
            "/api",
            {"method": method, "body": base64.b64encode(body).decode(), "content_type": kind},
        )


class Handler(BaseHTTPRequestHandler):
    server: "Transport"

    def setup(self) -> None:
        self.request.settimeout(60)
        super().setup()

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < length <= 3_000_000:
                raise ValueError("Invalid length")
            value = json.loads(self.rfile.read(length))
            result = self.server.handle(self.path, value)
            code = 200
        except Exception as exc:
            result = {"error": type(exc).__name__}
            code = 422
        raw = json.dumps(result).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Transport(socketserver.UnixStreamServer):
    def __init__(self, path: Path, state: Path, token: str, owner: str, caller: Any = api) -> None:
        self.state: Path = state
        self.token: str = token
        self.owner: str = owner
        self.caller: Any = caller
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS replies(key TEXT PRIMARY KEY,state TEXT NOT NULL,message_id INTEGER)"
            )
        path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if path.exists():
            if not stat.S_ISSOCK(path.lstat().st_mode) or path.lstat().st_uid != os.geteuid():
                raise ValueError("Unsafe socket")
            path.unlink()
        super().__init__(str(path), Handler)
        path.chmod(0o660)

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.state / "replies.sqlite", isolation_level=None)
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    def request(self, method: str, body: bytes = b"", kind: str = "application/json") -> Any:
        return self.caller(self.token, method, body, kind)

    def initialize(self) -> None:
        if self.request("getMe").get("username") != EXPECTED_BOT:
            raise ValueError("Wrong bot")
        if self.request("getWebhookInfo").get("url"):
            raise ValueError("Existing webhook; refusing to replace it")
        self.request(
            "setMyCommands",
            json.dumps(
                {
                    "commands": [
                        {"command": c, "description": d}
                        for c, d in [
                            ("weather", "طقس الرياض"),
                            ("status", "حالة التشغيل"),
                            ("help", "المساعدة"),
                        ]
                    ],
                    "scope": {"type": "chat", "chat_id": self.owner},
                }
            ).encode(),
        )

    def handle(self, path: str, value: Any) -> Any:
        if (
            path == "/poll"
            and set(value) == {"offset"}
            and type(value["offset"]) is int
            and value["offset"] >= 0
        ):
            updates = self.request(
                "getUpdates",
                json.dumps(
                    {
                        "offset": value["offset"],
                        "timeout": 20,
                        "limit": 20,
                        "allowed_updates": ["message"],
                    }
                ).encode(),
            )
            return normalize_updates(updates, self.owner, time.time())
        if path == "/reply" and set(value) == {"key", "message"}:
            key = value["key"]
            kind = value["message"]
            if (
                not isinstance(key, str)
                or not re.fullmatch("[A-Za-z0-9:_-]{1,100}", key)
                or kind not in MESSAGES
            ):
                raise ValueError("Invalid reply")
            with self.db() as db:
                if db.execute("SELECT 1 FROM replies WHERE key=?", (key,)).fetchone():
                    return {"replayed": False}
                db.execute("INSERT INTO replies VALUES(?,?,NULL)", (key, "SENDING"))
            # Mark before effect; a crash cannot cause an automatic duplicate.
            try:
                result = self.request(
                    "sendMessage",
                    json.dumps({"chat_id": self.owner, "text": MESSAGES[kind]}).encode(),
                )
                with self.db() as db:
                    db.execute(
                        "UPDATE replies SET state=?,message_id=? WHERE key=?",
                        ("SENT", result["message_id"], key),
                    )
                return {"message_id": result["message_id"]}
            except Exception:
                with self.db() as db:
                    db.execute("UPDATE replies SET state=? WHERE key=?", ("UNKNOWN", key))
                raise
        if path == "/api" and set(value) == {"method", "body", "content_type"}:
            method = value["method"]
            body = base64.b64decode(value["body"], validate=True)
            kind = value["content_type"]
            if method == "getMe" and not body:
                return self.request(method)
            if method == "getChat" and json.loads(body) == {"chat_id": self.owner}:
                return self.request(method, body)
            if method == "sendVideo":
                body, kind = validated_video(body, kind, self.owner)
                return self.request(method, body, kind)
        raise ValueError("RPC not allowed")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--socket", type=Path, required=True)
    p.add_argument("--state", type=Path, required=True)
    a = p.parse_args()
    try:
        with Transport(
            a.socket, a.state, os.environ["TG_TEST_TOKEN"], os.environ["TG_TEST_CHAT"]
        ) as server:
            server.initialize()
            print(json.dumps({"event": "telegram.egress.ready", "bot": EXPECTED_BOT}), flush=True)
            server.serve_forever()
    except Exception as exc:
        print(
            json.dumps({"event": "telegram.egress.failed", "error": type(exc).__name__}), flush=True
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
