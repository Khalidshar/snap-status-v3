"""Deterministic FFmpeg-only ambient bed; no recordings or external samples."""

import math
import re
from collections.abc import Callable
from typing import Any


def ambient_source(duration: float, *, page_count: int = 1) -> str:
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or type(page_count) is not int
        or not 1 <= page_count <= 15
        or not 8 <= duration / page_count <= 12
    ):
        raise ValueError("Invalid ambient duration")
    # Static A-minor pad; only a 40-second amplitude cycle, no notes or beats.
    frequencies = (220, 261.6256, 329.6276, 440, 659.2552)
    tones = "+".join(
        f"{weight:g}*sin(2*PI*{frequency:g}*t)"
        for weight, frequency in zip((0.65, 0.6, 0.7, 0.65, 0.2), frequencies, strict=True)
    )
    pad = f"0.06*({tones})*(0.96+0.04*sin(2*PI*t/40))"
    return (
        f"aevalsrc=exprs='{pad}':s=48000:d={duration:g}:c=stereo[pad];"
        f"anoisesrc=r=48000:d={duration:g}:c=pink:a=0.003:s=20261005[noise];"
        "[noise]pan=stereo|c0=c0|c1=c0[rain];"
        "[pad][rain]amix=inputs=2:normalize=0,volume=1.85,lowpass=f=2500:poles=2,"
        "afade=t=in:st=0:d=0.6,"
        f"afade=t=out:st={duration - 1:g}:d=1,atrim=duration={duration:g}"
    )


def measure_audio(
    path: object, profile: dict[str, Any], runner: Callable[..., Any], *, phone: bool = False
) -> dict[str, float]:
    filters = "highpass=f=300:poles=2,highpass=f=300:poles=2," if phone else ""
    result = runner(
        [
            profile["ffmpeg"],
            "-hide_banner",
            "-nostats",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            filters + "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        profile["timeout_seconds"],
    )
    summary = result.stderr.rsplit("Summary:", 1)[-1]
    integrated = re.search(r"\bI:\s*([+-]?[\d.]+|[+-]?inf)\s+LUFS", summary)
    peak = re.search(r"\bPeak:\s*([+-]?[\d.]+|[+-]?inf)\s+dBFS", summary)
    if not integrated or not peak:
        raise ValueError("Missing EBU R128 audio measurement")
    values = {"integrated_lufs": float(integrated[1]), "true_peak_dbtp": float(peak[1])}
    if not all(math.isfinite(v) for v in values.values()):
        raise ValueError("Non-finite ambient audio measurement")
    return values
