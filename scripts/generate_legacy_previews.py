#!/usr/bin/env python3
"""Manual real previews for both bots. No sending, schedules, keys or paid APIs."""

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from studio_core.config import Config
from studio_core.network import NetworkClient
from studio_core.network.production import ProductionPolicy
from studio_core.production.model import TITLES
from studio_core.production.prepare import prepare

KINDS = tuple(key for key in TITLES if key != "did_you_know")


def main() -> int:
    from typing import cast

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--app", choices=("snap", "status", "both"), default="both")
    parser.add_argument("--commands", nargs="+", choices=sorted(TITLES), default=KINDS)
    args = parser.parse_args()
    root = args.output.absolute()
    if ".." in root.parts or any(p.is_symlink() for p in (root, *root.parents)) or root.exists():
        parser.error("output must be a new directory with no symlinks")
    root.mkdir(parents=True, mode=0o700)
    socket = root / "source.sock"
    broker = subprocess.Popen(  # noqa: S603 — fixed broker argv
        [sys.executable, "-m", "studio_core.network.production", "--socket", str(socket)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    report = []
    try:
        deadline = time.monotonic() + 10
        while not socket.exists():
            if broker.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("SOURCE_BROKER_START_FAILED")
            time.sleep(0.05)
        client = NetworkClient(cast(Config, ProductionPolicy(socket)))
        for app in ("snap", "status") if args.app == "both" else (args.app,):
            for key in args.commands:
                target = root / app / key
                entry = {"app": app, "command": key, "external_delivery": False}
                try:
                    prepare(
                        app + "-bot-v2",
                        key,
                        uuid.uuid4().hex,
                        target / "input",
                        client=client,
                        state=root / app / "state",
                    )
                    subprocess.run(  # noqa: S603 — fixed offline renderer argv
                        [
                            sys.executable,
                            "-m",
                            "studio_core.production.render",
                            str(target / "input"),
                            str(target / "output"),
                        ],
                        check=True,
                        capture_output=True,
                        timeout=3600,
                    )
                    data = json.loads((target / "output/report.json").read_bytes())
                    entry.update(
                        {
                            "result": "PASS",
                            "video": str((target / "output/video.mp4").relative_to(root)),
                            "sha256": data["video_sha256"],
                            "qa": data["qa"],
                        }
                    )
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    entry.update(
                        {"result": "FAIL", "reason": type(error).__name__ + ": " + str(error)[:160]}
                    )
                report.append(entry)
                (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(app, key, entry["result"], flush=True)
    finally:
        broker.terminate()
        try:
            broker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            broker.kill()
            broker.wait(timeout=5)
        socket.unlink(missing_ok=True)
    return int(any(row["result"] != "PASS" for row in report))


if __name__ == "__main__":
    sys.exit(main())
