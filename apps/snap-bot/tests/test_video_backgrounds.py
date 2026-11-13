import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from studio_core.network.client import NetworkError, NetworkResponse
from studio_core.network.egress import PolicyDenied
from studio_core.network.video import VideoPolicy, fetch_video
from studio_core.presentation.video_backgrounds import prefetch, verify

MP4 = b"\x00\x00\x00\x18ftypisom" + b"0" * 20


def response(body, mime="application/json"):
    return NetworkResponse(200, mime, body, hashlib.sha256(body).hexdigest())


def test_fallback_hash_and_no_signed_url(tmp_path):
    calls = []

    def get(self, url):
        calls.append(url)
        if "pexels" in url:
            raise NetworkError("PROVIDER_NOT_CONFIGURED")
        if "pixabay.com/api" in url:
            return response(
                json.dumps(
                    {
                        "hits": [
                            {
                                "pageURL": "https://pixabay.com/videos/1/",
                                "videos": {
                                    "medium": {"url": "https://cdn.pixabay.com/a.mp4?token=hidden"}
                                },
                            }
                        ]
                    }
                ).encode()
            )
        return response(MP4, "video/mp4")

    with patch("studio_core.presentation.video_backgrounds.NetworkClient.get", get):
        manifest = prefetch("nature", tmp_path, Path("/unused"))
        assert verify(manifest).read_bytes() == MP4
        assert "hidden" not in manifest.read_text()
        assert "coverr" not in "".join(calls)
        assert prefetch("nature", tmp_path, Path("/unused")) == manifest
        verify(manifest).chmod(0o600)
        verify(manifest).write_bytes(b"changed")
        with pytest.raises(ValueError, match="ASSET_CHANGED"):
            verify(manifest)


@pytest.mark.parametrize(
    "url", ["http://cdn.pixabay.com/a", "https://localhost/a", "https://cdn.pixabay.com.evil/a"]
)
def test_denied_hosts(url):
    with pytest.raises(PolicyDenied):
        fetch_video(url, VideoPolicy(Path("/unused")))


@pytest.mark.parametrize(
    "status,size,mime",
    [(302, 10, "video/mp4"), (200, 50 * 1024 * 1024 + 1, "video/mp4"), (200, 5, "text/html")],
)
def test_gateway_rejections(status, size, mime):
    class Reply:
        def getheader(self, name, default=None):
            return str(size) if name == "Content-Length" else mime

        def read(self, count):
            raise AssertionError("must reject before reading")

    reply = Reply()
    reply.status = status
    with (
        patch(
            "studio_core.network.video.validate_target",
            return_value=("cdn.pixabay.com", "8.8.8.8", "/a"),
        ),
        patch("studio_core.network.video.PinnedHTTPSConnection") as conn,
    ):
        conn.return_value.getresponse.return_value = reply
        with pytest.raises(PolicyDenied):
            fetch_video("https://cdn.pixabay.com/a", VideoPolicy(Path("/unused")))


def test_authorization_only_to_api():
    with (
        patch(
            "studio_core.network.video.validate_target",
            return_value=("api.pexels.com", "8.8.8.8", "/v1/videos/search"),
        ),
        patch("studio_core.network.video.PinnedHTTPSConnection") as conn,
    ):
        reply = conn.return_value.getresponse.return_value
        reply.status = 200
        reply.getheader.side_effect = lambda name, default=None: (
            "2" if name == "Content-Length" else "application/json"
        )
        reply.read.return_value = b"{}"
        policy = VideoPolicy(Path("/unused"), secrets={"PEXELS_KEY": "fixture"})
        fetch_video("https://api.pexels.com/v1/videos/search", policy)
        assert conn.return_value.request.call_args.kwargs["headers"]["Authorization"] == "fixture"
        assert "fixture" not in repr(policy)
