"""Dedicated video egress; credentials never leave this process or enter logs."""

import argparse
import base64
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit

from studio_core.config import Config
from studio_core.network.egress import (
    EgressServer,
    PinnedHTTPSConnection,
    PolicyDenied,
    validate_target,
)

API = {
    "api.pexels.com": ("PEXELS_API_KEY", "/v1/videos/search", "Authorization", ""),
    "pixabay.com": ("PIXABAY_API_KEY", "/api/videos/", "", ""),
    "api.coverr.co": ("COVERR_API_KEY", "/videos", "Authorization", "Bearer "),
}
HOSTS = (*API, "videos.pexels.com", "cdn.pixabay.com", "storage.coverr.co")


@dataclass(frozen=True)
class VideoPolicy:
    proxy_socket: Path
    network_hosts: tuple[str, ...] = HOSTS
    network_timeout: int = 120
    network_max_bytes: int = 50 * 1024 * 1024
    network_user_agent: str = "madar-video/1"
    secrets: dict[str, str] = field(default_factory=dict, repr=False)


def fetch_video(url: str, config: VideoPolicy) -> dict[str, Any]:
    host, address, path = validate_target(url, config.network_hosts)
    headers = {"User-Agent": config.network_user_agent}
    limit = config.network_max_bytes
    if host in API:
        name, allowed_path, header, prefix = API[host]
        if urlsplit(url).path != allowed_path:
            raise PolicyDenied("API_PATH_DENIED")
        secret = config.secrets.get(name)
        if not secret:
            raise PolicyDenied("PROVIDER_NOT_CONFIGURED")
        if "\r" in secret or "\n" in secret:
            raise PolicyDenied("INVALID_CREDENTIAL")
        params = parse_qsl(urlsplit(url).query, keep_blank_values=True)
        if any(k.lower() in {"key", "api_key", "token", "authorization"} for k, _ in params):
            raise PolicyDenied("CALLER_CREDENTIAL_DENIED")
        if header:
            headers[header] = prefix + secret
        else:
            path = allowed_path + "?" + urlencode([*params, ("key", secret)])
        limit = 2 * 1024 * 1024
    connection = PinnedHTTPSConnection(host, address, config.network_timeout)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise PolicyDenied("UPSTREAM_REJECTED")
        length = response.getheader("Content-Length")
        if length and (not length.isdecimal() or int(length) > limit):
            raise PolicyDenied("RESPONSE_TOO_LARGE")
        mime = response.getheader("Content-Type", "").split(";")[0].lower()
        if mime != ("application/json" if host in API else "video/mp4"):
            raise PolicyDenied("CONTENT_TYPE_DENIED")
        body = response.read(limit + 1)
        if not body or len(body) > limit:
            raise PolicyDenied("RESPONSE_SIZE_DENIED")
        return {
            "status": response.status,
            "content_type": mime,
            "body": base64.b64encode(body).decode(),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args()
    secrets = {name: os.environ[name] for name, _, _, _ in API.values() if name in os.environ}
    policy = VideoPolicy(args.socket, secrets=secrets)
    # The existing RPC server needs only the transport attributes of Config.
    with EgressServer(cast(Config, policy), lambda url, _: fetch_video(url, policy)) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
