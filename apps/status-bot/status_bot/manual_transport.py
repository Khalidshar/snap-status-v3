"""Bounded owner-only Status test egress. No polling or command changes."""

import argparse
import base64
import email.policy
import hashlib
import json
import os
import re
import socket
import socketserver
import struct
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from studio_core.network import UnixHTTPConnection

from . import platform_policy
from .telegram_egress import EXPECTED_BOT, api, multipart


def peer_uid(sock):
    return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


class Client:
    def __init__(self, path, server_uid):
        self.path = path
        self.server_uid = server_uid

    def telegram(self, ignored_token, method, body=b"", kind="application/json"):
        platform_policy.reject_status_telegram()
        if method not in {"getMe", "getChat", "sendVideo"}:
            raise ValueError("Unsupported method")
        connection = UnixHTTPConnection(self.path, 60)
        try:
            connection.connect()
            if peer_uid(connection.sock) != self.server_uid:
                raise ValueError("Wrong egress identity")
            value = {
                "method": method,
                "body": base64.b64encode(body).decode(),
                "content_type": kind,
            }
            connection.request(
                "POST", "/api", json.dumps(value).encode(), {"Content-Type": "application/json"}
            )
            response = connection.getresponse()
            raw = response.read(1048577)
            if len(raw) > 1048576 or response.status != 200:
                raise RuntimeError("Status egress refused")
            return json.loads(raw)
        finally:
            connection.close()


def approved_video(body, kind, owner, digest):
    if not re.fullmatch(r"multipart/form-data; boundary=studio[a-f0-9]{32}", kind):
        raise ValueError("Invalid multipart")
    mail = BytesParser(policy=email.policy.default).parsebytes(
        ("Content-Type: " + kind + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body
    )
    parts = list(mail.iter_parts())
    names = [p.get_param("name", header="content-disposition") for p in parts]
    if sorted(names) != sorted(["chat_id", "caption", "supports_streaming", "video"]):
        raise ValueError("Invalid fields")
    fields = dict(zip(names, parts))
    if fields["chat_id"].get_payload(decode=True).decode() != owner:
        raise ValueError("Wrong owner")
    video = fields["video"].get_payload(decode=True)
    if (
        fields["video"].get_content_type() != "video/mp4"
        or not 0 < len(video) <= 2_000_000
        or hashlib.sha256(video).hexdigest() != digest
    ):
        raise ValueError("Unapproved video")
    return multipart(owner, video)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(60)
        if peer_uid(self.request) != self.server.worker_uid:
            raise ValueError("Wrong worker identity")
        super().setup()

    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            size = self.headers.get("Content-Length", "")
            if (
                self.path != "/api"
                or self.headers.get("Transfer-Encoding")
                or not size.isdecimal()
                or not 0 < int(size) <= 3_000_000
            ):
                raise ValueError("Invalid request")
            body = self.rfile.read(int(size))
            if len(body) != int(size):
                raise ValueError("Truncated frame")
            result = self.server.handle(json.loads(body))
            code = 200
        except Exception:
            result = {"error": "STATUS_EGRESS_REFUSED"}
            code = 422
        raw = json.dumps(result).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Transport(socketserver.UnixStreamServer):
    def __init__(self, path, worker_uid, token, owner, digest, caller=api):
        platform_policy.reject_status_telegram()
        if not re.fullmatch("[0-9]+", owner) or not re.fullmatch("[a-f0-9]{64}", digest):
            raise ValueError("Invalid pinned acceptance")
        self.worker_uid = worker_uid
        self.token = token
        self.owner = owner
        self.digest = digest
        self.caller = caller
        if path.exists() or path.is_symlink():
            raise ValueError("Existing test socket")
        super().__init__(str(path), Handler)
        path.chmod(0o660)

    def handle(self, value):
        if not isinstance(value, dict) or set(value) != {"method", "body", "content_type"}:
            raise ValueError("Invalid schema")
        method = value["method"]
        body = base64.b64decode(value["body"], validate=True)
        kind = value["content_type"]
        if method == "getMe":
            if body:
                raise ValueError("Unexpected identity payload")
            result = self.caller(self.token, "getMe")
            if result.get("username") != EXPECTED_BOT:
                raise ValueError("Wrong test bot")
            return result
        if method == "getChat":
            if json.loads(body) != {"chat_id": self.owner}:
                raise ValueError("Wrong owner")
            return self.caller(self.token, "getChat", json.dumps({"chat_id": self.owner}).encode())
        if method == "sendVideo":
            safe, kind = approved_video(body, kind, self.owner, self.digest)
            return self.caller(self.token, "sendVideo", safe, kind)
        raise ValueError("Unsupported method")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--worker-uid", type=int, required=True)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    with Transport(
        args.socket,
        args.worker_uid,
        os.environ["TG_TEST_TOKEN"],
        os.environ["TG_TEST_CHAT"],
        args.sha,
    ) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
