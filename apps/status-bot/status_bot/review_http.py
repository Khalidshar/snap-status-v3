"""Separate owner/webhook AF_UNIX services; explicit trusted assembly only.

TLS termination and authorization to deploy are host acceptance gates. This
module never opens a TCP listener, loads Graph tokens, or runs delivery workers.
"""

import hmac
import html
import os
import re
import socket
import socketserver
import stat
import struct
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from studio_core.jobs import LeaseLost

from .whatsapp_contract import DeliveryBlocked, require

COOKIE = "__Host-status_owner"
CSS = """*{box-sizing:border-box}body{margin:0;background:#f2f5f1;color:#173529;font:18px system-ui,sans-serif}
main{max-width:880px;margin:48px auto;padding:32px;background:white;border:1px solid #d8e2d9;border-radius:24px}
header{color:#647b6b;letter-spacing:.03em}h1{font-size:32px}p{line-height:1.8}label{display:block;margin:20px 0}
input{font:inherit;padding:12px;border:1px solid #b9cabb;border-radius:8px;max-width:100%}
button{font:inherit;background:#205a40;color:white;border:0;border-radius:10px;padding:12px 24px;cursor:pointer}
button.secondary{background:#edf2ed;color:#244731}video{width:100%;max-height:65vh;background:#12241a;border-radius:16px}
article{padding:20px 0;border-bottom:1px solid #e2e9e1}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.8}
.actions{display:flex;gap:16px;flex-wrap:wrap;margin-top:24px}a{color:#205a40}small{color:#647b6b}
@media(max-width:600px){main{margin:12px;padding:20px}h1{font-size:26px}}"""


def page(title, content):
    return (
        '<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>" + html.escape(title) + '</title><link rel="stylesheet" href="/style.css"></head>'
        "<body><main><header>مدار · مراجعة المقاطع</header><h1>"
        + html.escape(title)
        + "</h1>"
        + content
        + "</main></body></html>"
    ).encode()


def fields(raw):
    require(type(raw) is bytes and len(raw) <= 8192, "HTTP_FORM_INVALID")
    try:
        text = raw.decode("utf-8", errors="strict")
        require(not re.search(r"%(?![0-9a-fA-F]{2})", text), "HTTP_FORM_INVALID")
        pairs = parse_qsl(
            text,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=8,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeError, ValueError):
        raise DeliveryBlocked("HTTP_FORM_INVALID") from None
    require(len({k for k, _ in pairs}) == len(pairs), "HTTP_FORM_INVALID")
    return dict(pairs)


@dataclass(frozen=True, repr=False)
class WebhookConfig:
    host: str
    verify_token: bytes

    def __post_init__(self):
        require(
            type(self.host) is str
            and re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", self.host)
            and type(self.verify_token) is bytes
            and 32 <= len(self.verify_token) <= 512
            and all(33 <= c <= 126 for c in self.verify_token),
            "WEBHOOK_CONFIG_INVALID",
        )


class Endpoint:
    def __init__(self, mode, service, config=None):
        require(mode in ("owner", "webhook"), "HTTP_MODE_INVALID")
        self.mode, self.service, self.config = mode, service, config
        if mode == "owner":
            self.host = urlsplit(service.config.origin).netloc
        else:
            require(isinstance(config, WebhookConfig), "WEBHOOK_CONFIG_INVALID")
            require(
                config.verify_token
                not in (service.identity.gate_key, service.identity.webhook_key),
                "WEBHOOK_KEYS_INVALID",
            )
            self.host = config.host

    def dispatch(self, method, target, headers, body):
        require(headers.get("Host") == self.host, "HTTP_HOST_DENIED")
        parsed = urlsplit(target)
        require(
            not parsed.scheme
            and not parsed.netloc
            and not parsed.fragment
            and target.startswith("/")
            and len(target) <= 2048,
            "HTTP_TARGET_INVALID",
        )
        if self.mode == "webhook":
            require(parsed.path == "/whatsapp/webhook", "HTTP_ROUTE_DENIED")
            if method == "GET":
                value = fields(parsed.query.encode())
                require(
                    set(value) == {"hub.mode", "hub.verify_token", "hub.challenge"}
                    and value["hub.mode"] == "subscribe"
                    and re.fullmatch("[0-9]{1,128}", value["hub.challenge"])
                    and hmac.compare_digest(
                        value["hub.verify_token"].encode(), self.config.verify_token
                    ),
                    "WEBHOOK_VERIFY_DENIED",
                )
                return 200, "text/plain; charset=utf-8", value["hub.challenge"].encode(), {}
            require(
                method == "POST"
                and not parsed.query
                and headers.get("Content-Type") == "application/json",
                "HTTP_ROUTE_DENIED",
            )
            self.service.webhook(body, headers.get("X-Hub-Signature-256", ""))
            return 200, "text/plain; charset=utf-8", b"OK", {}
        require(not parsed.query, "HTTP_TARGET_INVALID")
        if method == "GET" and parsed.path == "/style.css":
            return 200, "text/css; charset=utf-8", CSS.encode(), {}
        if method == "GET" and parsed.path == "/login":
            return (
                200,
                "text/html; charset=utf-8",
                page(
                    "دخول المالك",
                    '<form method="post" action="/login"><label>اسم المستخدم '
                    '<input name="owner" autocomplete="username" required></label>'
                    '<label>مفتاح الدخول <input type="password" name="secret" '
                    'autocomplete="current-password" required></label><button>دخول</button></form>',
                ),
                {},
            )
        value = None
        if method == "POST":
            require(headers.get("Origin") == self.service.config.origin, "HTTP_ORIGIN_DENIED")
            require(
                headers.get("Content-Type") == "application/x-www-form-urlencoded",
                "HTTP_FORM_INVALID",
            )
            value = fields(body)
        if method == "POST" and parsed.path == "/login":
            require(set(value) == {"owner", "secret"}, "HTTP_FORM_INVALID")
            cookie = self.service.login(value["owner"], value["secret"].encode())
            return (
                303,
                "text/plain",
                b"",
                {
                    "Location": "/",
                    "Set-Cookie": COOKIE
                    + "="
                    + cookie
                    + "; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=900",
                },
            )
        cookies = []
        for part in headers.get("Cookie", "").split(";"):
            key, separator, val = part.strip().partition("=")
            if key == COOKIE and separator:
                cookies.append(val)
        require(len(cookies) == 1, "REVIEW_SESSION_DENIED")
        cookie = cookies[0]
        csrf = self.service.csrf(cookie)
        hidden = '<input type="hidden" name="csrf" value="' + csrf + '">'
        if method == "GET" and parsed.path == "/":
            rows = self.service.pending(cookie)
            content = "<p>راجع المقطع قبل اعتماده للتسليم عبر WhatsApp.</p>"
            for row in rows:
                content += (
                    '<article><form method="post" action="/review">'
                    + hidden
                    + '<input type="hidden" name="job" value="'
                    + row["id"]
                    + '">'
                    + "<button>فتح مقطع الطقس</button></form></article>"
                )
            if not rows:
                content += "<p>لا توجد مقاطع تنتظر المراجعة.</p>"
            content += (
                '<form method="post" action="/logout">'
                + hidden
                + '<p><button class="secondary">خروج</button></p></form>'
            )
            return 200, "text/html; charset=utf-8", page("المقاطع المنتظرة", content), {}
        if method == "POST" and parsed.path == "/logout":
            require(set(value) == {"csrf"}, "HTTP_FORM_INVALID")
            self.service.logout(cookie, value["csrf"])
            return (
                303,
                "text/plain",
                b"",
                {
                    "Location": "/login",
                    "Set-Cookie": COOKIE
                    + "=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0",
                },
            )
        if method == "POST" and parsed.path == "/review":
            require(set(value) == {"csrf", "job"}, "HTTP_FORM_INVALID")
            ticket = self.service.open(cookie, value["csrf"], value["job"])
            return 303, "text/plain", b"", {"Location": "/review/" + ticket}
        match = re.fullmatch("/review/([0-9a-f]{32})(/media|/decision)?", parsed.path)
        require(match is not None, "HTTP_ROUTE_DENIED")
        ticket, suffix = match.groups()
        if method == "GET" and suffix == "/media":
            return 200, "video/mp4", self.service.media(cookie, ticket), {"Accept-Ranges": "none"}
        if method == "GET" and suffix is None:
            _, _, manifest, _ = self.service.view(cookie, ticket)
            # All source content is escaped. No raw tokens, IDs, leases or signatures.
            source = manifest["content"]
            content = (
                '<video controls preload="metadata" src="/review/' + ticket + '/media"></video>'
            )
            content += (
                "<h2>"
                + html.escape(str(source["title"]))
                + "</h2><pre>"
                + html.escape(str(source["body"]))
                + "</pre>"
            )
            content += "<p><small>" + html.escape(str(source["attribution"])) + " · "
            content += html.escape(str(source["model_time"])) + "</small></p>"
            content += (
                '<form method="post" action="/review/'
                + ticket
                + '/decision">'
                + hidden
                + '<label><input type="checkbox" name="watched" value="yes"> شاهدت المقطع كاملًا وأوافق على محتواه.</label>'
                + '<div class="actions"><button name="decision" value="approve">اعتماد للتسليم</button>'
                + '<button class="secondary" name="decision" value="reject">رفض المقطع</button></div></form>'
                + "<p><small>الاعتماد يجهّز المقطع للتسليم. نشر الحالة يتم يدويًا.</small></p>"
            )
            return 200, "text/html; charset=utf-8", page("مراجعة مقطع الطقس", content), {}
        require(
            method == "POST"
            and suffix == "/decision"
            and set(value) in ({"csrf", "decision"}, {"csrf", "decision", "watched"})
            and value.get("watched") in (None, "yes"),
            "HTTP_FORM_INVALID",
        )
        result = self.service.decide(
            cookie, value["csrf"], ticket, value["decision"], value.get("watched") == "yes"
        )
        title = "تم اعتماد المقطع" if result["state"] == "PREPARED" else "تم رفض المقطع"
        return (
            200,
            "text/html; charset=utf-8",
            page(title, '<p><a href="/">العودة إلى المقاطع</a></p>'),
            {},
        )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "Madar"
    sys_version = ""

    def setup(self):
        uid = struct.unpack(
            "3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )[1]
        require(uid == self.server.proxy_uid, "HTTP_PEER_DENIED")
        self.request.settimeout(10)
        super().setup()

    def log_message(self, *args):
        pass  # No URL, login body, challenge token, cookie or exception logging.

    def send_error(self, code, message=None, explain=None):
        self.reply(code, "text/plain; charset=utf-8", b"Request denied", {})

    def reply(self, status, content_type, body, extra):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self'; media-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def handle_endpoint(self):
        try:
            require(
                sum(len(k) + len(v) for k, v in self.headers.items()) <= 16384,
                "HTTP_HEADERS_INVALID",
            )
            for key in (
                "Host",
                "Content-Length",
                "Content-Type",
                "Cookie",
                "Origin",
                "X-Hub-Signature-256",
            ):
                require(len(self.headers.get_all(key, [])) <= 1, "HTTP_HEADERS_INVALID")
            require(
                not self.headers.get_all("Transfer-Encoding")
                and not self.headers.get_all("Content-Encoding")
                and not self.headers.get_all("Expect"),
                "HTTP_FRAMING_INVALID",
            )
            length = self.headers.get("Content-Length")
            if self.command == "POST":
                require(
                    length is not None and re.fullmatch("[0-9]{1,7}", length),
                    "HTTP_FRAMING_INVALID",
                )
                size = int(length)
                limit = 8192 if self.server.endpoint.mode == "owner" else 1048576
                require(0 < size <= limit, "HTTP_BODY_INVALID")
            else:
                require(length in (None, "0"), "HTTP_FRAMING_INVALID")
                size = 0
            body = self.rfile.read(size)
            require(len(body) == size, "HTTP_BODY_INVALID")
            response = self.server.endpoint.dispatch(self.command, self.path, self.headers, body)
        except (DeliveryBlocked, LeaseLost):
            response = (403, "text/plain; charset=utf-8", b"Request denied", {})
        except Exception:
            response = (503, "text/plain; charset=utf-8", b"Service unavailable", {})
        self.reply(*response)

    do_GET = handle_endpoint
    do_POST = handle_endpoint


class PrivateServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, socket_path, endpoint, proxy_uid, *, enabled=False):
        require(enabled is True, "HTTP_SERVICE_DISABLED")
        require(
            type(proxy_uid) is int and proxy_uid >= 0 and isinstance(endpoint, Endpoint),
            "HTTP_CONFIG_INVALID",
        )
        path = Path(socket_path).absolute()
        require(path.name and len(str(path).encode()) <= 100, "HTTP_SOCKET_INVALID")
        for ancestor in (path.parent, *path.parent.parents):
            require(not ancestor.is_symlink(), "HTTP_SOCKET_INVALID")
        parent = path.parent.stat()
        require(
            stat.S_ISDIR(parent.st_mode)
            and parent.st_uid == os.getuid()
            and stat.S_IMODE(parent.st_mode) == 0o700
            and not path.exists()
            and not path.is_symlink(),
            "HTTP_SOCKET_INVALID",
        )
        self.endpoint, self.proxy_uid = endpoint, proxy_uid
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(str(path), Handler)
        os.chmod(path, 0o600)

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

    def handle_error(self, request, client_address):
        pass  # Fail closed without secret-bearing tracebacks.
