"""The old free Edge voice, only for verified riddle answers.

Quran, dua and adhkar cannot enter this endpoint. No keys or paid AI APIs.
"""

import json
from urllib.parse import parse_qs, urlsplit

from studio_core.network.egress import PolicyDenied

HOST = "speech.platform.bing.com"


def approved(url: str) -> str:
    from studio_core.production.model import DATA

    parts = urlsplit(url)
    query = parse_qs(parts.query, strict_parsing=True)
    if (
        parts.scheme != "tts"
        or parts.hostname != "riddle"
        or parts.path
        or set(query) != {"text"}
        or len(query["text"]) != 1
    ):
        raise PolicyDenied("PRODUCTION_SPEECH_SCOPE_DENIED")
    text = query["text"][0]
    allowed = [i["a"] for i in json.loads((DATA / "riddles.json").read_bytes())]
    if text not in allowed:
        raise PolicyDenied("PRODUCTION_SPEECH_TEXT_DENIED")
    return text


def fetch(url: str) -> bytes:
    import os
    import subprocess
    from pathlib import Path

    approved(url)
    root = Path(__file__).resolve().parents[4]
    python = Path(os.environ.get("MADAR_LEGACY_VOICE_PYTHON", "/opt/madar/legacy-voice/bin/python"))
    if not python.is_absolute() or not python.is_file():
        raise PolicyDenied("PRODUCTION_VOICE_ENV_MISSING")
    try:
        result = subprocess.run(  # noqa: S603 — trusted operator-selected interpreter, fixed worker
            [str(python), str(root / "scripts/legacy_speech_worker.py")],
            input=json.dumps({"url": url}).encode(),
            capture_output=True,
            timeout=120,
            check=True,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(root / "packages/studio-core"),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
    except subprocess.SubprocessError:
        raise PolicyDenied("PRODUCTION_SPEECH_FAILED") from None
    if not 0 < len(result.stdout) <= 4_000_000:
        raise PolicyDenied("PRODUCTION_SPEECH_SIZE_DENIED")
    return result.stdout
