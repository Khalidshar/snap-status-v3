"""Render a stored hadith through real Pango and inspect its transparent PNG pixels."""

import shutil
import struct
import zlib
from pathlib import Path

import pytest

from snap_bot import faith, sacred_text

FONT = (
    Path(__file__).resolve().parents[3] / "assets/fonts/notonaskharabic/NotoNaskhArabic-Regular.ttf"
)


def _alpha_pixels(png: bytes) -> list[int]:
    """Decode Cairo's 8-bit RGBA scanlines, including PNG row filters."""
    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", png[16:29]
    )
    assert (depth, color, compression, filtering, interlace) == (8, 6, 0, 0, 0)
    offset, compressed = 8, bytearray()
    while offset < len(png):
        length = struct.unpack(">I", png[offset : offset + 4])[0]
        if png[offset + 4 : offset + 8] == b"IDAT":
            compressed.extend(png[offset + 8 : offset + 8 + length])
        offset += length + 12
    raw = zlib.decompress(compressed)
    stride = width * 4
    assert len(raw) == height * (stride + 1)
    previous = bytearray(stride)
    alpha = []
    for y in range(height):
        at = y * (stride + 1)
        mode = raw[at]
        assert mode in range(5)
        row = bytearray(raw[at + 1 : at + 1 + stride])
        for x in range(stride):
            left = row[x - 4] if x >= 4 else 0
            above = previous[x]
            corner = previous[x - 4] if x >= 4 else 0
            predictor = 0
            if mode == 1:
                predictor = left
            elif mode == 2:
                predictor = above
            elif mode == 3:
                predictor = (left + above) // 2
            elif mode == 4:
                estimate = left + above - corner
                predictor = min((left, above, corner), key=lambda v: abs(estimate - v))
            row[x] = (row[x] + predictor) & 255
        alpha.extend(row[3::4])
        previous = row
    return alpha


def test_real_hadith_card_png(tmp_path: Path) -> None:
    executable = shutil.which("pango-view")
    if executable is None:
        pytest.skip("pango-view is not installed")
    # Select an entire, short stored narration without editing its sacred text.
    item = next(item for item in faith.load("hadith") if item["id"] == "bukhari_13")
    output = tmp_path / "hadith.png"
    dimensions = sacred_text.render_text_png(
        faith.text(item),
        FONT,
        output,
        pango_view=executable,
        width_px=820,
        max_height_px=660,
    )
    png = output.read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    assert dimensions == (width, height)
    assert 0 < width <= 820 and 0 < height <= 660
    alpha = _alpha_pixels(png)
    assert any(alpha), "The transparent card contains no text pixels"
    assert 0 in alpha, "The card should retain transparent background pixels"
