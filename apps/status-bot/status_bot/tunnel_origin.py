"""Loopback-only bridge from a scoped Cloudflare VPC Service to the existing Unix inbox."""

import argparse
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from studio_core.config import runtime_state_root
from studio_core.network.client import UnixHTTPConnection

LIMIT = 262144


def forward(path: str, headers: Any, body: bytes, *, host: str, socket: Path) -> tuple[int, bytes]:
    if (
        path != "/whatsapp/webhook"
        or headers.get("Host") != host
        or headers.get("Content-Type") != "application/json"
        or headers.get("Transfer-Encoding")
        or not 0 < len(body) <= LIMIT
        or not re.fullmatch(r"sha256=[0-9a-f]{64}", headers.get("X-Hub-Signature-256", ""))
    ):
        return 403, b"DENIED"
    connection = UnixHTTPConnection(socket, 8)
    try:
        connection.request(
            "POST",
            path,
            body,
            {
                "Host": host,
                "Content-Type": "application/json",
                "X-Hub-Signature-256": headers["X-Hub-Signature-256"],
            },
        )
        result = connection.getresponse()
        result.read(1024)
        # The Unix endpoint performs the second HMAC check and inbox commit.
        return (200, b"OK") if result.status == 200 else (503, b"INBOX_UNAVAILABLE")
    finally:
        connection.close()


def serve(address: str, port: int, host: str, socket: Path) -> None:
    if (
        address != "127.0.0.1"
        or not 1024 <= port <= 65535
        or not re.fullmatch(r"[a-z0-9.-]+", host)
    ):
        raise ValueError("Loopback receiver only")
    if not socket.is_absolute() or any(p.is_symlink() for p in (socket, *socket.parents)):
        raise ValueError("Unsafe inbox socket")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            self.connection.settimeout(10)
            length = self.headers.get("Content-Length", "")
            if not length.isdecimal() or not 0 < int(length) <= LIMIT:
                self.send_error(413)
                return
            raw = self.rfile.read(int(length))
            try:
                status, body = forward(self.path, self.headers, raw, host=host, socket=socket)
            except (OSError, ValueError):
                status, body = 503, b"INBOX_UNAVAILABLE"
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    HTTPServer((address, port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="madar-v3-whatsapp.internal")
    parser.add_argument(
        "--socket", type=Path, default=runtime_state_root() / "status/command-webhook/incoming.sock"
    )
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve("127.0.0.1", args.port, args.host, args.socket)


if __name__ == "__main__":
    main()
