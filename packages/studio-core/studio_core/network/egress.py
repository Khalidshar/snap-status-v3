"""Small GET-only foundation proxy; exact hosts, public DNS, pinned TLS, no redirects."""

import base64
import hashlib
import http.client
import ipaddress
import json
import os
import socket
import socketserver
import ssl
import stat
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

from studio_core.observability import json_line


class PolicyDenied(ValueError):
    pass


def validate_target(url, hosts, resolver=socket.getaddrinfo):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise PolicyDenied("INVALID_URL")
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise PolicyDenied("INVALID_URL") from None
    if (
        parsed.scheme != "https"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise PolicyDenied("HTTPS_ONLY")
    if host not in hosts:
        raise PolicyDenied("HOST_NOT_ALLOWED")
    records = resolver(host, 443, type=socket.SOCK_STREAM)
    addresses = sorted({record[4][0] for record in records})
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise PolicyDenied("NON_PUBLIC_ADDRESS")
    # All answers are checked; connection uses one fixed address with original TLS SNI.
    return host, addresses[0], (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def fetch_https(url, config):
    host, address, path = validate_target(url, config.network_hosts)
    connection = PinnedHTTPSConnection(host, address, config.network_timeout)
    try:
        connection.request("GET", path, headers={"User-Agent": config.network_user_agent})
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise PolicyDenied("REDIRECT_DENIED")
        length = response.getheader("Content-Length")
        if length and (not length.isdecimal() or int(length) > config.network_max_bytes):
            raise PolicyDenied("RESPONSE_TOO_LARGE")
        body = response.read(config.network_max_bytes + 1)
        if len(body) > config.network_max_bytes:
            raise PolicyDenied("RESPONSE_TOO_LARGE")
        return {
            "status": response.status,
            "content_type": response.getheader("Content-Type", "")[:256],
            "body": base64.b64encode(body).decode(),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    finally:
        connection.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.request.settimeout(self.server.config.network_timeout)
        super().setup()

    def log_message(self, *args):
        pass

    def reply(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            if self.path != "/fetch" or self.headers.get("Transfer-Encoding"):
                raise PolicyDenied("INVALID_RPC")
            length = self.headers.get("Content-Length", "")
            if not length.isdecimal() or not 0 < int(length) <= 8192:
                raise PolicyDenied("INVALID_RPC")
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                raise PolicyDenied("INVALID_RPC")
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {"url"}:
                raise PolicyDenied("INVALID_RPC")
            result = self.server.fetcher(value["url"], self.server.config)
            print(
                json_line(
                    "network.response",
                    status=result["status"],
                    bytes=len(base64.b64decode(result["body"])),
                ),
                flush=True,
            )
            self.reply(200, result)
        except PolicyDenied as exc:
            print(json_line("network.denied", code=str(exc)), flush=True)
            self.reply(403, {"error": str(exc)})
        except (OSError, ValueError, http.client.HTTPException):
            print(json_line("network.error", code="FETCH_FAILED"), flush=True)
            self.reply(502, {"error": "FETCH_FAILED"})


class EgressServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, config, fetcher=fetch_https):
        self.config, self.fetcher = config, fetcher
        self.slots = threading.BoundedSemaphore(2)
        config.proxy_socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if config.proxy_socket.exists() or config.proxy_socket.is_symlink():
            old = config.proxy_socket.lstat()
            if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.geteuid():
                raise ValueError("Unsafe proxy socket")
            config.proxy_socket.unlink()
        super().__init__(str(config.proxy_socket), Handler)
        config.proxy_socket.chmod(0o660)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()
