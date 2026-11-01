#!/usr/bin/env python3
"""فحص المصادر للأوامر الثمانية فقط؛ لا إرسال ولا تعديل سجل منع التكرار."""

import argparse
import json
from pathlib import Path

from studio_core.config import Config
from studio_core.network import NetworkClient
from studio_core.source_checks import check
from studio_core.sources.news import TITLES


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/snap/news.toml"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = check(NetworkClient(Config.load(args.config)))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(f"{TITLES[row['command']]} | {row['name']} | {row['state']}")


if __name__ == "__main__":
    main()
