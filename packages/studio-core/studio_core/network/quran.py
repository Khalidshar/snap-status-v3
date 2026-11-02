"""QF-only egress process. OAuth credentials exist here, never in render workers."""

import argparse
import base64
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from studio_core.config import Config
from studio_core.network.egress import EgressServer, PinnedHTTPSConnection, validate_target

ENVIRONMENTS = {
    "prelive": ("prelive-oauth2.quran.foundation", "apis-prelive.quran.foundation"),
    "production": ("oauth2.quran.foundation", "apis.quran.foundation"),
}
CDN_HOSTS = ("verses.quran.com", "audio.qurancdn.com")
MAX_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class QuranPolicy:
    proxy_socket: Path
    environment: str = "prelive"
    production_approved: bool = False
    client_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    network_timeout: int = 90
    network_max_bytes: int = MAX_BYTES
    network_user_agent: str = "madar-quran/1"

    def __post_init__(self) -> None:
        if self.environment not in ENVIRONMENTS:
            raise ValueError("QF_ENV_INVALID")
        if self.environment == "production" and not self.production_approved:
            raise ValueError("QF_PRODUCTION_NOT_APPROVED")
        if (
            not self.client_id
            or not self.client_secret
            or any(c in self.client_id + self.client_secret for c in "\r\n")
        ):
            raise ValueError("QF_CREDENTIALS_MISSING_OR_INVALID")


class QuranEgress:
    def __init__(self, policy: QuranPolicy) -> None:
        self.policy = policy
        self.token = ""
        self.expires = 0.0
        self.lock = threading.Lock()

    def _request(
        self,
        url: str,
        hosts: tuple[str, ...],
        headers: dict[str, str],
        *,
        method: str = "GET",
        body: bytes | None = None,
    ) -> tuple[int, str, bytes]:
        host, address, path = validate_target(url, hosts)
        conn = PinnedHTTPSConnection(host, address, self.policy.network_timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            length = response.getheader("Content-Length")
            if length and (not length.isdecimal() or int(length) > MAX_BYTES):
                raise ValueError("QF_RESPONSE_TOO_LARGE")
            data = response.read(MAX_BYTES + 1)
            if not data or len(data) > MAX_BYTES:
                raise ValueError("QF_RESPONSE_SIZE_INVALID")
            return response.status, response.getheader("Content-Type", "").split(";")[0], data
        except Exception:
            # Do not retain upstream exception text, request headers, bodies or URLs.
            raise ValueError("QF_TRANSPORT_FAILED") from None
        finally:
            conn.close()

    def _token(self) -> str:
        with self.lock:
            if time.monotonic() < self.expires:
                return self.token
            host, _ = ENVIRONMENTS[self.policy.environment]
            basic = base64.b64encode(
                (self.policy.client_id + ":" + self.policy.client_secret).encode()
            ).decode()
            status, mime, body = self._request(
                "https://" + host + "/oauth2/token",
                (host,),
                {
                    "Authorization": "Basic " + basic,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="POST",
                body=b"grant_type=client_credentials&scope=content",
            )
            if status != 200 or mime != "application/json":
                raise ValueError("QF_AUTH_REJECTED")
            try:
                value = json.loads(body)
                token = value["access_token"]
                seconds = float(value["expires_in"])
                if not isinstance(token, str) or not token or not 60 <= seconds <= 86400:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise ValueError("QF_AUTH_INVALID") from None
            self.token, self.expires = token, time.monotonic() + seconds - 30
            return token

    def fetch(self, url: str, _: Any = None) -> dict[str, Any]:
        _, api = ENVIRONMENTS[self.policy.environment]
        parsed = urlsplit(url)
        is_api = parsed.hostname == api
        if is_api and not (
            parsed.path
            in {
                "/content/api/v4/resources/recitations",
                "/content/api/v4/resources/tafsirs",
                "/content/api/v4/resources/sync",
            }
            or parsed.path.startswith("/content/api/v4/resources/snapshots/recitations/")
            or parsed.path.startswith("/content/api/v4/resources/snapshots/tafsirs/")
        ):
            raise ValueError("QF_API_PATH_DENIED")
        if not is_api and (parsed.query or not parsed.path.endswith(".mp3")):
            raise ValueError("QF_AUDIO_PATH_DENIED")
        # No redirects; never send OAuth credentials to a CDN or another environment.
        for attempt in range(2):
            headers = {"User-Agent": self.policy.network_user_agent}
            if is_api:
                headers.update(
                    {"x-auth-token": self._token(), "x-client-id": self.policy.client_id}
                )
            status, mime, body = self._request(url, (api,) if is_api else CDN_HOSTS, headers)
            if status == 401 and is_api and attempt == 0:
                self.expires = 0
                continue
            if status != 200:
                raise ValueError("QF_HTTP_" + str(status))
            if mime not in ({"application/json"} if is_api else {"audio/mpeg", "audio/mp3"}):
                raise ValueError("QF_CONTENT_TYPE_INVALID")
            return {
                "status": status,
                "content_type": mime,
                "body": base64.b64encode(body).decode(),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        raise ValueError("QF_AUTH_REJECTED")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True, type=Path)
    args = parser.parse_args()
    # systemd loads /etc/madar/quran.env; never parse or print that file here.
    policy = QuranPolicy(
        args.socket,
        environment=os.environ.get("QF_ENV", "prelive"),
        production_approved=os.environ.get("QF_PRODUCTION_APPROVED") == "yes",
        client_id=os.environ.get("QF_CLIENT_ID", ""),
        client_secret=os.environ.get("QF_CLIENT_SECRET", ""),
    )
    egress = QuranEgress(policy)
    with EgressServer(cast(Config, policy), egress.fetch) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
