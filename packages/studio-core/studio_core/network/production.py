"""GET-only source broker for the old production mechanism. Never a renderer."""

import argparse
import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

from studio_core.config import Config
from studio_core.network.egress import (
    EgressServer,
    PinnedHTTPSConnection,
    PolicyDenied,
    validate_target,
)

ROOT = Path(__file__).resolve().parents[4]
BASE = {
    "archive.org",
    "mp3quran.net",
    "www.mp3quran.net",
    "everyayah.com",
    "quranenc.com",
    "api.quran.com",
    "hisnmuslim.com",
    "www.hisnmuslim.com",
    "api.aladhan.com",
    "api.open-meteo.com",
    "air-quality-api.open-meteo.com",
    "api.met.no",
    "trends.google.com",
    "translate.googleapis.com",
    "cdn.pixabay.com",
    "free-stock-music.com",
    "www.free-stock-music.com",
    "www.chosic.com",
    "www.bbc.com",
    "bbc.com",
    "www.bbc.co.uk",
    "bbc.co.uk",
    "assets.vogue.com",
    "media.allure.com",
    "hips.hearstapps.com",
    "assets.adidas.com",
    "brand.assets.adidas.com",
    "fimgs.net",
    "ichef.bbci.co.uk",
    "ichef.bbc.co.uk",
    "news.bbcimg.co.uk",
    "static.euronews.com",
    "s.france24.com",
    "media.france24.com",
    "vid.alarabiya.net",
    "images.skynewsarabia.com",
    "www.skynewsarabia.com",
    "media.assettype.com",
    "assets.arabnews.com",
    "arabic.arabianbusiness.com",
    "aawsat.com",
    "static.srpcdigital.com",
    "www.sayidaty.net",
    "www.harpersbazaararabia.com",
    "ar.harpersbazaararabia.com",
    "aitnews.com",
    "www.tech-wd.com",
    "img.youm7.com",
    "www.ellearabia.com",
    "ar.wikipedia.org",
    "upload.wikimedia.org",
}
SUFFIXES = (".mp3quran.net", ".archive.org")
MIMES = {
    "application/json",
    "text/json",
    "text/html",
    "application/xhtml+xml",
    "text/xml",
    "application/xml",
    "application/rss+xml",
    "application/atom+xml",
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "application/octet-stream",
    "image/jpeg",
    "image/png",
    "image/webp",
    "video/mp4",
}


def hosts() -> set[str]:

    # Publisher/feeds from the approved registry only; do not fetch a new source merely
    # because a page asks us to. Dedicated image CDNs are listed separately above.
    catalog = json.loads((ROOT / "config/media/legacy-production.json").read_text())
    from studio_core.sources.panel import hosts as panel_hosts

    # Owner decision 9 Nov 2026: a source Khalid approves in the panel opens its host.
    return BASE | set(catalog["hosts"]) | panel_hosts()


@dataclass(frozen=True)
class ProductionPolicy:
    proxy_socket: Path
    network_timeout: int = 90
    network_max_bytes: int = 128 * 1024 * 1024
    network_user_agent: str = "Rawwag/3.0 (+https://github.com/Khalidshar/snap-status-v3)"


def permitted(url: str) -> str:
    host = urlsplit(url).hostname or ""
    if host not in hosts() and not any(host.endswith(s) for s in SUFFIXES):
        raise PolicyDenied("PRODUCTION_SOURCE_HOST_DENIED")
    # Every redirection, CDN and DNS answer is validated and connected by pinned IP.
    validate_target(url, (host,))
    return host


def fetch(url: str, config: ProductionPolicy) -> dict[str, Any]:
    if url.startswith("tts:"):
        from .production_speech import fetch as speech

        body = speech(url)
        return {
            "status": 200,
            "content_type": "audio/mpeg",
            "body": base64.b64encode(body).decode(),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    for _ in range(4):
        host = permitted(url)
        _, address, path = validate_target(url, (host,))
        connection = PinnedHTTPSConnection(host, address, config.network_timeout)
        try:
            headers = {"User-Agent": config.network_user_agent}
            if host in {"www.free-stock-music.com", "free-stock-music.com"}:
                # The site serves its MP3s to visitors of its own pages (one theme per kind).
                headers["Referer"] = "https://www.free-stock-music.com/"
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise PolicyDenied("PRODUCTION_REDIRECT_INVALID")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise PolicyDenied("PRODUCTION_SOURCE_UNAVAILABLE")
            mime = response.getheader("Content-Type", "").split(";")[0].strip().lower()
            if mime not in MIMES:
                raise PolicyDenied("PRODUCTION_MIME_DENIED")
            limit = (
                config.network_max_bytes
                if mime.startswith("audio/") or mime == "application/octet-stream"
                else 12 * 1024 * 1024
            )
            length = response.getheader("Content-Length")
            if length and (not length.isdecimal() or int(length) > limit):
                raise PolicyDenied("PRODUCTION_SIZE_DENIED")
            body = response.read(limit + 1)
            if not body or len(body) > limit:
                raise PolicyDenied("PRODUCTION_SIZE_DENIED")
            return {
                "status": 200,
                "content_type": mime,
                "body": base64.b64encode(body).decode(),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        finally:
            connection.close()
    raise PolicyDenied("PRODUCTION_REDIRECT_LIMIT")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--fetch")
    args = parser.parse_args()
    policy = ProductionPolicy(args.socket or Path("/run/madar/production.sock"))
    if args.fetch:
        print(json.dumps(fetch(args.fetch, policy)))
        return
    if not args.socket:
        parser.error("--socket or --fetch required")
    with EgressServer(cast(Config, policy), lambda url, _: fetch(url, policy)) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
