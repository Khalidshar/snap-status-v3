"""Lossless text pagination and isolated Pango PNGs. No network or video rendering."""

import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from xml.etree import ElementTree as ET


def _text(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("Empty or invalid text")
    # U+06DD is the official Quran ayah marker, not a directional control.
    # Multiple markers occur when exact ayah texts are joined for display.
    if any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} and c != "\u06dd" for c in value):
        raise ValueError("Control character in sacred text")


def _positive(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("Expected positive integer")


def pages(text: str, max_chars: int) -> list[str]:
    """Split only at literal spaces; joining with one space restores exact bytes.

    Repeated/boundary spaces stay in the page strings, including empty boundary
    pages when necessary. An unbreakable word over the limit is refused.
    """
    _text(text)
    _positive(max_chars)
    result: list[str] = []
    start = 0
    while len(text) - start > max_chars:
        boundary = text.rfind(" ", start, start + max_chars + 1)
        if boundary < start:
            raise ValueError("Word exceeds page character limit")
        result.append(text[start:boundary])
        start = boundary + 1
    result.append(text[start:])
    return result


def _dimensions(image: Path) -> tuple[int, int]:
    with image.open("rb") as source:
        header = source.read(33)
    if (
        len(header) != 33
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[8:16] != b"\x00\x00\x00\rIHDR"
    ):
        raise ValueError("Pango did not produce a PNG")
    width, height = struct.unpack(">II", header[16:24])
    if width < 1 or height < 1:
        raise ValueError("Empty Pango image")
    return width, height


def render_text_png(
    text: str,
    font_path: str | Path,
    out_path: str | Path,
    *,
    pango_view: str | Path,
    max_height_px: int,
    width_px: int = 820,
    font_size: int = 44,
    color: str = "#F6EEDE",
    layout_path: Path | None = None,
) -> tuple[int, int]:
    """Render exact UTF-8 through Pango, then admit the PNG within the height cap.

    The height limit is mandatory. No cropping, ellipsizing, markup, text edits,
    font installation or system-font fallback. Output is replaced only on success.
    Requires fc-query from fontconfig as well as the caller-specified pango-view.
    """
    _text(text)
    for number in (width_px, font_size, max_height_px):
        _positive(number)
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        raise ValueError("Invalid text color")
    font = Path(font_path).resolve(strict=True)
    output = Path(out_path).absolute()
    if layout_path is not None:
        layout_path = Path(layout_path).absolute()
        if layout_path in (output, font):
            raise ValueError("Invalid layout output path")
    if not font.is_file() or output.resolve() == font:
        raise ValueError("Invalid font or output path")
    executable = shutil.which(str(pango_view))
    if executable is None:
        raise ValueError("pango-view executable not found")
    fc_query = shutil.which("fc-query")
    if fc_query is None:
        raise ValueError("fc-query executable not found")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sacred-text-", dir=output.parent) as directory:
        root = Path(directory)
        fonts = root / "fonts"
        fonts.mkdir()
        selected = fonts / font.name
        shutil.copyfile(font, selected)  # exact copy, no rewriting or conversion
        cache = root / "cache"
        cache.mkdir()
        config = ET.Element("fontconfig")
        ET.SubElement(config, "reset-dirs")
        ET.SubElement(config, "dir").text = str(fonts)
        ET.SubElement(config, "cachedir").text = str(cache)
        config_path = root / "fonts.conf"
        ET.ElementTree(config).write(config_path, encoding="utf-8", xml_declaration=True)
        env = {
            "FONTCONFIG_FILE": str(config_path),
            "FONTCONFIG_PATH": str(root),
            "XDG_CACHE_HOME": str(cache),
            "PANGOCAIRO_BACKEND": "fontconfig",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        family_query = subprocess.run(  # noqa: S603
            [fc_query, "--format=%{family}", str(selected)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        family = family_query.stdout.strip().split(",")[0]
        if not family or any(unicodedata.category(c).startswith("C") for c in family):
            raise ValueError("Invalid font family")
        input_file = root / "text.txt"
        input_file.write_bytes(text.encode("utf-8"))
        image = root / "text.png"
        args = [
            executable,
            "--no-display",
            "--backend=cairo",
            "--rtl",
            "--align=center",
            "--wrap=word",
            "--background=transparent",
            "--pixels",
            "--dpi=72",
            "--margin=0",
            f"--width={width_px}",
            f"--font={family} {font_size}",
            f"--foreground={color}",
            f"--output={image}",
            *([f"--serialize-to={root / 'layout.json'}"] if layout_path else []),
            "--",
            str(input_file),
        ]
        subprocess.run(  # noqa: S603
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        dimensions = _dimensions(image)
        if dimensions[1] > max_height_px:
            raise ValueError("Sacred text image exceeds maximum height")
        if layout_path is not None:
            metadata = json.loads((root / "layout.json").read_text())
            rendered = metadata["output"]
            if (
                metadata.get("text") != text
                or rendered.get("unknown-glyphs") != 0
                or rendered.get("is-ellipsized") is not False
                or not rendered.get("lines")
            ):
                raise ValueError("Invalid Pango line layout")
            os.replace(root / "layout.json", layout_path)
        os.replace(image, output)
        return dimensions
