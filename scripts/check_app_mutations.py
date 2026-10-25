"""Guard deletions for the Snap app, each in an isolated temporary source copy.

261 cases are the lab's named mutation tools (snap-bot-v2 tools/check_*_mutations.py),
recorded and re-expressed against the formatted v3 sources; later cases guard v3-only
code such as the main delivery channel. Every case removes or
weakens one protective line; its named test must then fail, and when the lab case
names an assertion marker, that marker must appear in the failure.
Usage: uv run --frozen python scripts/check_app_mutations.py [--jobs N] [--only TOOL]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "apps" / "snap-bot"
CASES = json.loads((ROOT / "scripts" / "snap_app_mutations.json").read_text(encoding="utf-8"))


def check(case: dict[str, Any]) -> tuple[bool, str]:
    label = f"{case['tool']}: {case['label']}"
    with tempfile.TemporaryDirectory(prefix="v3-app-mutation-") as temp:
        # Keep the repository depth so parents[3] still finds root assets and config.
        for shared in ("assets", "config"):
            (Path(temp) / shared).symlink_to(ROOT / shared)
        package = Path(temp) / "apps" / "snap-bot"
        copy = package / "snap_bot"
        shutil.copytree(APP / "snap_bot", copy, ignore=shutil.ignore_patterns("__pycache__"))
        for hunk in case["hunks"]:
            source = copy / hunk["file"]
            text = source.read_text(encoding="utf-8")
            if text.count(hunk["old"]) != 1:
                return False, f"Mutation target not unique: {label}"
            source.write_text(text.replace(hunk["old"], hunk["new"]), encoding="utf-8")
        path = os.pathsep.join(filter(None, (str(package), os.environ.get("PYTHONPATH"))))
        env = {**os.environ, "PYTHONPATH": path, "PYTHONDONTWRITEBYTECODE": "1"}
        # Fixed argv: this interpreter running the repository's own app tests.
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "pytest",
                str(APP / "tests"),
                "-q",
                "-p",
                "no:cacheprovider",
                "-k",
                case["test"],
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=ROOT,
            timeout=900,
        )
        output = result.stdout + result.stderr
        failed = result.returncode != 0 and any(
            # pytest 9 reports a failing unittest subTest as "SUBFAILED[...] path::test".
            line.startswith(("FAILED ", "ERROR ", "SUBFAILED")) and f"::{case['test']}" in line
            for line in output.splitlines()
        )
        if not failed or (case["marker"] and case["marker"] not in output):
            return False, f"Named guard sample not detected: {label}\n{output[-2500:]}"
    return True, f"app guard deletion detected: {label}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--only")
    args = parser.parse_args()
    cases = [c for c in CASES if not args.only or c["tool"] == args.only]
    misses = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for ok, message in pool.map(check, cases):
            print(message, flush=True)
            misses += not ok
    print(f"app guard samples detected: {len(cases) - misses}/{len(cases)}")
    return 1 if misses else 0


if __name__ == "__main__":
    raise SystemExit(main())
