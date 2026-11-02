"""Manual per-bot prepare/render adapter. It never sends or schedules anything."""

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from studio_core.config import Config
from studio_core.network import NetworkClient
from studio_core.religious.quran_foundation import CATALOG, Source, Store, prepare


@dataclass(frozen=True)
class QuranSocketPolicy:
    proxy_socket: Path
    network_timeout: int = 100
    network_max_bytes: int = 32 * 1024 * 1024


def prepare_clip(
    app: str,
    root: Path,
    job: str,
    surah: int,
    ayah: int,
    *,
    socket: Path,
    environment: str = "prelive",
    catalog: Path = CATALOG,
) -> Path:
    client = NetworkClient(cast(Config, QuranSocketPolicy(socket)))
    return prepare(Store(root, app), Source(client, environment), job, surah, ayah, catalog)


def main(app: str) -> None:
    parser = argparse.ArgumentParser(description="مقاطع التلاوة والتفسير — دون إرسال أو جدولة")
    parser.add_argument("action", choices=["prepare", "render", "retention"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--job")
    parser.add_argument("--surah", type=int)
    parser.add_argument("--ayah", type=int)
    parser.add_argument("--environment", choices=["prelive", "production"], default="prelive")
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    args = parser.parse_args()
    if args.action == "retention":
        store = Store(args.root, app)
        while True:
            store.sweep()
            time.sleep(30)  # Asset lifecycle only; never creates content or sends messages.
    if args.job is None:
        parser.error("يلزم معرّف المقطع")
    result: Any
    if args.action == "prepare":
        if args.surah is None or args.ayah is None:
            parser.error("يلزم رقم السورة والآية")
        socket = Path(
            "/run/madar/" + ("snap" if app == "snap-bot-v2" else "status") + "-quran.sock"
        )
        result = {
            "bundle": str(
                prepare_clip(
                    app,
                    args.root,
                    args.job,
                    args.surah,
                    args.ayah,
                    socket=socket,
                    environment=args.environment,
                    catalog=args.catalog,
                )
            )
        }
    else:
        from studio_core.presentation.quran_clip import render

        store = Store(args.root, app)
        store.sweep()
        result = render(store.directory(args.job) / "bundle.json", app)
    # Paths and hashes only. No source text, audio URL, upstream errors or credentials.
    print(
        json.dumps({k: result[k] for k in ("bundle", "video_sha256", "expires_at") if k in result})
    )
