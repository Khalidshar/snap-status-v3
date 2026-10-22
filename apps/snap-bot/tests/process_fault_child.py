"""Disposable subprocess fixture. No live network, secrets or production paths."""

import argparse
import json
import os
import socketserver
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from studio_core.config import Config
from studio_core.jobs import Repository


def signal_ready(name):
    print(name, flush=True)


def park(name):
    signal_ready(name)
    threading.Event().wait(60)
    raise RuntimeError("Parent failed to terminate checkpoint process")


def load(path):
    state = json.loads(Path(path).read_text())
    config = replace(Config.load(Path(state["config"])), data_root=Path(state["data_root"]))
    return state, config, Repository(config)


def quality(state, config, repo, mode):
    from snap_bot.owned_contract_adapter import run_stage, telemetry
    from snap_bot.render_commit import adopt_render, record_stages

    claim = state["claim"]
    job = claim["id"]
    token = claim["lease_token"]
    attempt = claim["attempt"]

    def staged(*args):
        park("CUT:fsynced-before-db")

    def observed(*args):
        if args[4] == "quality" and args[6] == "SUCCEEDED":
            park("CUT:db-before-telemetry")
        return telemetry(*args)

    with (
        patch(
            "snap_bot.render_commit.record_stages",
            side_effect=staged if mode == "fsynced-before-db" else record_stages,
        ),
        patch("snap_bot.owned_contract_adapter.telemetry", side_effect=observed),
    ):
        run_stage(
            repo,
            job,
            token,
            attempt,
            "quality",
            state["result"],
            lambda: adopt_render(
                config, repo, job, token, attempt, state["result"], state["settings"]
            ),
        )


def partial(state):
    from snap_bot.render import write_once

    real_fdopen = os.fdopen

    class PartialWrite:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def write(self, data):
            self.stream.write(data[: len(data) // 2])
            self.stream.flush()
            # Real bytes are written; no fsync has occurred at this checkpoint.
            park("CUT:write-before-fsync")

        def __getattr__(self, name):
            return getattr(self.stream, name)

    with patch(
        "snap_bot.render.os.fdopen",
        side_effect=lambda fd, mode: PartialWrite(real_fdopen(fd, mode)),
    ):
        write_once(Path(state["video"]), bytes.fromhex(state["video_hex"]))


def server(state, config):
    from test_render_ownership import ISOLATION

    from snap_bot.service import renderer

    root = Path(state["control"])
    root.mkdir(exist_ok=True)
    lock = threading.Lock()
    count = 0

    def fake_run(args, timeout):
        nonlocal count
        if "-version" in args:
            return Mock(stdout="ffmpeg synthetic\nbuild")
        if args[0] == "fc-query":
            return Mock(stdout="0-10ffff")
        if str(args[-1]).endswith(".partial.mp4"):
            with lock:
                count += 1
                (root / "encode-count").write_text(str(count))
                signal_ready("ENCODE:" + str(count))
            deadline = time.monotonic() + 15
            while not (root / "release").exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("Socket race was not released")
                time.sleep(0.01)
        Path(args[-1]).write_bytes(bytes.fromhex(state["video_hex"]))
        return Mock(stdout="")

    activate = socketserver.UnixStreamServer.server_activate

    def ready(instance):
        activate(instance)
        signal_ready("SOCKET_READY")

    with (
        patch("socketserver.UnixStreamServer.server_activate", ready),
        patch("snap_bot.render.run", side_effect=fake_run),
        patch("snap_bot.render.proof", return_value=ISOLATION),
        patch("snap_bot.render.validate", return_value={"result": "PASS"}),
        patch(
            "snap_bot.render.measure_label",
            return_value={"x": 200, "y": 650, "width": 600, "height": 80},
        ),
    ):
        renderer(config, state["settings"])


def client(state):
    from studio_core.network import UnixHTTPConnection

    connection = UnixHTTPConnection(state["settings"]["render_socket"], 15)
    try:
        connection.request(
            "POST",
            "/render",
            json.dumps(
                {
                    "job_id": state["claim"]["id"],
                    "attempt": 1,
                    "content_hash": state["result"]["content_hash"],
                }
            ),
        )
        signal_ready("REQUEST_SENT")
        response = connection.getresponse()
        body = response.read()
        print(json.dumps({"status": response.status, "result": json.loads(body)}), flush=True)
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("state")
    parser.add_argument("mode")
    args = parser.parse_args()
    state, config, repo = load(args.state)
    if args.mode == "write-before-fsync":
        partial(state)
    elif args.mode in ("fsynced-before-db", "db-before-telemetry"):
        quality(state, config, repo, args.mode)
    elif args.mode == "server":
        server(state, config)
    elif args.mode == "client":
        client(state)
    else:
        raise ValueError("Unknown disposable fixture mode")
