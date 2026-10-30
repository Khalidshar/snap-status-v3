"""Two independent stages: Network Core collection, then an offline app-owned card."""

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from studio_core.config import Config
from studio_core.network.client import NetworkClient
from studio_core.renderer.primitives import write_once
from studio_core.renderer.text_card import render as render_card
from studio_core.sources.news import collect, validate


def _new_directory(path: Path) -> Path:
    path = path.absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Symlink review workspace")
    path.mkdir(mode=0o700, exist_ok=False)
    return path


def prepare(app: str, command: str, network_config: Path, output: Path) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        raise FileExistsError("News review output already exists")
    config = Config.load(network_config)
    if app not in {"snap-bot", "status-bot"} or config.app != app + "-v2":
        raise ValueError("News source app mismatch")
    result = collect(command, NetworkClient(config), time.time())
    envelope = {"app": app, **result}
    root = _new_directory(output)
    write_once(root / "content.json", json.dumps(envelope, ensure_ascii=False).encode())
    return {"input": str(root / "content.json"), "external_delivery": False}


def render(
    app: str,
    input_file: Path,
    output: Path,
    settings: dict[str, Any],
    layout: Callable[[dict[str, Any]], list[tuple[str, int, int]]],
) -> dict[str, Any]:
    if app not in {"snap-bot", "status-bot"} or settings.get("app") != app:
        raise ValueError("News renderer app mismatch")
    if any(p.is_symlink() for p in (input_file, *input_file.absolute().parents)):
        raise ValueError("Symlink news input")
    with input_file.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("News input too large")
    envelope = json.loads(raw)
    if (
        not isinstance(envelope, dict)
        or envelope.get("app") != app
        or envelope.get("external_delivery") is not False
        or not isinstance(envelope.get("content"), dict)
    ):
        raise ValueError("Foreign news input")
    content = envelope["content"]
    validate(content, time.time())
    labels = layout(content)
    root = _new_directory(output)
    write_once(root / "content.json", raw)
    result = render_card(labels, root, settings)
    validate(content, time.time())
    result.update(app=app, content_hash=content["content_hash"], external_delivery=False)
    write_once(root / "render-result.json", json.dumps(result, ensure_ascii=False).encode())
    return {"output": str(root), **result}
