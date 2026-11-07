"""Telegram sends state the clip's shape (owner report 7 Nov 2026: square display)."""

import shutil
import subprocess
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from studio_core.delivery.evidence import multipart, video_shape


def test_unreadable_video_adds_no_shape_fields() -> None:
    assert video_shape(b"not an mp4") == {}
    body, _kind = multipart(1, b"not an mp4", "c", "v.mp4")
    assert b'name="width"' not in body


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_portrait_clip_is_announced_as_1080x1920(tmp_path: Path) -> None:
    out = tmp_path / "v.mp4"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    subprocess.run(  # noqa: S603  # fixed argv, test file
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=green:s=1080x1920:d=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
        check=True,
    )  # fmt: skip
    video = out.read_bytes()
    assert video_shape(video) == {"width": "1080", "height": "1920", "duration": "3"}
    body, kind = multipart(7, video, "c", "v.mp4")
    mail = BytesParser(policy=policy.default).parsebytes(
        ("Content-Type: " + kind + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body
    )
    fields = {
        p.get_param("name", header="content-disposition"): p.get_content()
        for p in mail.iter_parts()
        if p.get_content_type() == "text/plain"
    }
    assert fields["width"].strip() == "1080" and fields["height"].strip() == "1920"
