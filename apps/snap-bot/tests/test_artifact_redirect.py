"""Local two-host HTTP fixture behind a logical HTTPS handler; no public I/O."""

import hashlib
import http.client
import http.server
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
from unittest.mock import patch

from snap_bot import send_reconciliation as rec


@contextmanager
def servers(
    *,
    location="https://storage.example.invalid/archive?sig=SYNTHETIC",
    storage_redirect=False,
    raw=None,
):
    if raw is None:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("intent.json", json.dumps({"state": "synthetic"}))
        raw = output.getvalue()
    observed = {"github": [], "storage": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never expose paths or signed URLs.

        def do_GET(self):
            kind = self.server.kind
            observed[kind].append(dict(self.headers))
            if kind == "github" or storage_redirect:
                self.send_response(302)
                self.send_header("Location", location)
                self.end_headers()
                return
            if self.headers.get("Authorization"):
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"Server failed to authenticate the request.")
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    hosts = {}
    running = []
    for kind, hostname in [("github", "api.github.com"), ("storage", "storage.example.invalid")]:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.kind = kind
        thread = threading.Thread(
            target=lambda server=server: server.serve_forever(poll_interval=0.01), daemon=True
        )
        thread.start()
        hosts[hostname] = server.server_port
        running.append((server, thread))

    class LocalHTTPS(urllib.request.HTTPSHandler):
        def https_open(self, request):
            # Keep urllib Request/redirect/header behavior real; replace only TCP/TLS.
            hostname = request.host.split(":")[0]
            if hostname not in hosts:
                raise AssertionError("Unexpected fixture host")
            return self.do_open(
                lambda host, timeout=30, **kwargs: http.client.HTTPConnection(
                    "127.0.0.1", hosts[hostname], timeout=timeout
                ),
                request,
            )

    original = urllib.request.build_opener

    def opener(*handlers):
        return original(urllib.request.ProxyHandler({}), LocalHTTPS(), *handlers)

    try:
        yield observed, raw, opener
    finally:
        for server, thread in running:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


class ArtifactRedirectTests(unittest.TestCase):
    def artifact(self, raw):
        return {"id": 9, "expired": False, "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}

    def test_old_urllib_add_header_leaks_to_redirected_host_and_gets_401(self):
        with servers() as (observed, raw, opener):
            request = urllib.request.Request("https://api.github.com/synthetic")
            request.add_header("Authorization", "Bearer SYNTHETIC")
            request.add_header("Accept", "application/vnd.github+json")
            with self.assertRaises(urllib.error.HTTPError) as error:
                opener().open(request, timeout=30)
            self.assertEqual(error.exception.code, 401, "guard:old-redirect-401")
            self.assertEqual(
                observed["storage"][0].get("Authorization"),
                "Bearer SYNTHETIC",
                "guard:old-redirect-auth-reproduction",
            )
            self.assertEqual(observed["storage"][0].get("Accept"), "application/vnd.github+json")

    def test_new_two_request_download_has_no_github_headers_and_enforces_digest(self):
        with (
            servers() as (observed, raw, opener),
            patch("urllib.request.build_opener", side_effect=opener),
        ):
            try:
                result = rec.archive_json(self.artifact(raw), {"GITHUB_TOKEN": "SYNTHETIC"})
            except Exception as exc:
                self.fail("guard:redirect-no-auth " + type(exc).__name__)
            self.assertEqual(result, {"intent.json": {"state": "synthetic"}})
            self.assertEqual(len(observed["github"]), 1)
            self.assertEqual(len(observed["storage"]), 1)
            self.assertEqual(observed["github"][0].get("Authorization"), "Bearer SYNTHETIC")
            self.assertEqual(observed["github"][0].get("Accept"), "application/vnd.github+json")
            lowered = {key.lower() for key in observed["storage"][0]}
            self.assertFalse(
                lowered & {"authorization", "accept", "x-github-api-version"},
                "guard:redirect-no-auth",
            )
            with self.assertRaisesRegex(
                ValueError, "Evidence digest mismatch", msg="guard:redirect-digest"
            ):
                rec.archive_json(
                    {**self.artifact(raw), "digest": "sha256:" + "0" * 64},
                    {"GITHUB_TOKEN": "SYNTHETIC"},
                )

    def test_insecure_redirect_rejected_before_second_request(self):
        with (
            servers(location="http://storage.example.invalid/archive") as (observed, raw, opener),
            patch("urllib.request.build_opener", side_effect=opener),
        ):
            with self.assertRaisesRegex(
                ValueError, "Unsafe artifact redirect", msg="guard:redirect-https"
            ):
                rec.archive_json(self.artifact(raw), {"GITHUB_TOKEN": "SYNTHETIC"})
            self.assertEqual(observed["storage"], [])

    def test_second_request_does_not_follow_further_redirects(self):
        with (
            servers(storage_redirect=True) as (observed, raw, opener),
            patch("urllib.request.build_opener", side_effect=opener),
        ):
            with self.assertRaisesRegex(ValueError, "Unexpected artifact download status"):
                rec.archive_json(self.artifact(raw), {"GITHUB_TOKEN": "SYNTHETIC"})
            self.assertEqual(len(observed["storage"]), 1)

    def test_original_archive_size_limit_still_enforced(self):
        with (
            servers(raw=b"x" * 2_000_001) as (observed, raw, opener),
            patch("urllib.request.build_opener", side_effect=opener),
        ):
            try:
                rec.archive_json(self.artifact(raw), {"GITHUB_TOKEN": "SYNTHETIC"})
            except ValueError as exc:
                self.assertEqual(str(exc), "Evidence oversized", "guard:redirect-size")
            except Exception as exc:
                self.fail("guard:redirect-size " + type(exc).__name__)
            else:
                self.fail("guard:redirect-size size bound missing")
