"""Computation-stage observation. Never wraps delivery or retries publication."""

import json
import re
import subprocess
import time
import uuid
from pathlib import Path

NODE_EXECUTABLE = "/usr/bin/node"


class TelemetryUnavailable(RuntimeError):
    pass


class TelemetryReconciliationRequired(TelemetryUnavailable):
    code = "TELEMETRY_RECONCILIATION_REQUIRED"


def _record(config, event):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", event["job"]):
        raise TelemetryUnavailable("Invalid event identity")
    script = Path(__file__).parent / "madar" / "record.mjs"
    try:
        completed = subprocess.run(
            [
                NODE_EXECUTABLE,
                "--max-old-space-size=32",
                str(script),
                str(config.data_root / "madar"),
                config.app,
            ],
            input=json.dumps(event),
            text=True,
            capture_output=True,
            check=False,
            timeout=8,
            env={"PATH": "/usr/bin:/bin"},
        )
        if completed.returncode == 3:
            raise TelemetryReconciliationRequired("Stage replay requires reconciliation")
        if completed.returncode != 0:
            raise TelemetryUnavailable("Agent telemetry unavailable")
    except (OSError, subprocess.SubprocessError):
        raise TelemetryUnavailable("Agent telemetry unavailable") from None


def observe(config, agent, job, callback):
    event = {"agent": agent, "job": job, "run": uuid.uuid4().hex}
    _record(config, {**event, "status": "RUNNING"})
    started = time.monotonic()
    try:
        result = callback()
    except BaseException:
        _record(
            config,
            {
                **event,
                "status": "FAILED",
                "duration_ms": int((time.monotonic() - started) * 1000),
                "error_code": "STAGE_FAILED",
            },
        )
        raise
    _record(
        config,
        {**event, "status": "SUCCEEDED", "duration_ms": int((time.monotonic() - started) * 1000)},
    )
    return result
