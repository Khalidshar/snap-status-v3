"""The real JS receiver and the private bridge boundaries, without network or deployment."""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from status_bot.tunnel_origin import forward, serve

ROOT = Path(__file__).resolve().parents[3]


def test_real_worker_challenge_signature_and_forwarding():
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(
        [node, "--test", str(ROOT / "workers/madar-v3-whatsapp/worker.test.mjs")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_bridge_forwards_exact_bytes_and_requires_unix_ack(tmp_path):
    headers = {
        "Host": "madar-v3-whatsapp.internal",
        "Content-Type": "application/json",
        "X-Hub-Signature-256": "sha256=" + "a" * 64,
    }
    connection = Mock()
    connection.getresponse.return_value.status = 200
    with patch("status_bot.tunnel_origin.UnixHTTPConnection", return_value=connection):
        assert forward(
            "/whatsapp/webhook",
            headers,
            b"{}",
            host=headers["Host"],
            socket=tmp_path / "inbox.sock",
        ) == (200, b"OK")
        assert connection.request.call_args.args[2] == b"{}"
        connection.getresponse.return_value.status = 403
        assert (
            forward(
                "/whatsapp/webhook",
                headers,
                b"{}",
                host=headers["Host"],
                socket=tmp_path / "inbox.sock",
            )[0]
            == 503
        )
        assert (
            forward("/wrong", headers, b"{}", host=headers["Host"], socket=tmp_path / "inbox.sock")[
                0
            ]
            == 403
        )


@pytest.mark.parametrize("address", ["0.0.0.0", "::", "192.168.1.1"])  # noqa: S104
def test_bridge_cannot_listen_outside_loopback(address, tmp_path):
    with pytest.raises(ValueError, match="Loopback"):
        serve(address, 8765, "madar-v3-whatsapp.internal", tmp_path / "inbox.sock")
