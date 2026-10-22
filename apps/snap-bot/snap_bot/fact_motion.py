"""Fact-only reveal timing and a cached, low-opacity glow; no content changes."""

import math
import re

READING_SECONDS = 1.5
SLIDE_PIXELS = 20


def background_filters(filters):
    # Interpolate before 8-bit quantization, then diffuse the rounding error.
    # Grain is generated once before loop caching, never on text or per frame.
    filters = list(filters)
    clock = filters[4] if len(filters) == 6 and isinstance(filters[4], str) else ""
    expected = [
        "format=gbrp",
        "geq=r='11+(6-11)*Y/(H-1)':g='61+(42-61)*Y/(H-1)':b='46+(32-46)*Y/(H-1)'",
        "format=yuv420p",
        "loop=loop=-1:size=1:start=0",
        clock,
        "drawbox=x=80:y=340:w=920:h=1240:color=0xD6B26C:t=2",
    ]
    if filters != expected or not re.fullmatch(r"setpts=N/\(([1-9]|[1-5][0-9]|60)\*TB\)", clock):
        raise ValueError("Unexpected card filter shape; background replacement refused")
    filters[0] = "format=gbrp16le"
    filters[1] = (
        "geq=r='257*(11+(6-11)*Y/(H-1))':g='257*(61+(42-61)*Y/(H-1))':b='257*(46+(32-46)*Y/(H-1))'"
    )
    filters[2:3] = [
        "scale=iw:ih:sws_dither=ed",
        "format=yuv420p",
        "noise=alls=3:allf=u:all_seed=20261004",
    ]
    return filters


def reveal_windows(label_count, duration):
    body_count = label_count - 5
    if not 1 <= body_count <= 5 or not 8 <= duration <= 12:
        raise ValueError("Invalid fact motion profile")
    windows = [(0.0, 0.6), (0.0, 0.6), (0.6, 1.2)]
    windows += [(1.2 + 0.5 * i, 1.7 + 0.5 * i) for i in range(body_count)]
    start = 1.2 + 0.5 * body_count
    windows += [(start, start + 0.5), (start + 0.5, start + 1.0)]
    if windows[-1][1] > duration - READING_SECONDS:
        raise ValueError("Fact has no full reading hold")
    return windows


def label_filter(filter_value, window):
    start, end = window
    progress = f"clip((t-{start:g})/{end - start:g},0,1)"
    # Begin above the final baseline so the footer never slides below SAFE.
    prefix, baseline = filter_value.rsplit(":y=", 1)
    return prefix + f":y='{baseline}-{SLIDE_PIXELS}*(1-{progress})':alpha='{progress}'"


def glow_position(t, duration):
    clock = min(max(t, 0), duration - READING_SECONDS)
    return (240 + 30 * math.sin(clock / 30), 450 + 20 * math.sin(clock / 30))


def glow_filter(fps, duration):
    # A continuous radial falloff reaches zero at the tile edge. Spatial
    # stochastic rounding avoids concentric 8-bit alpha rings; its coordinate
    # hash is fixed, so cached alpha does not flicker or disturb the final hold.
    hold = duration - READING_SECONDS
    return (
        "split[card][glow];[glow]trim=end_frame=1,crop=600:600:0:0,"
        "format=rgba,geq=r=214:g=178:b=108:"
        "a='floor(6*max(0,(exp(-((X-300)*(X-300)+(Y-300)*(Y-300))/30000)"
        "-exp(-3))/(1-exp(-3)))+mod(abs(sin(X*12.9898+Y*78.233)*43758.5453),1))',"
        f"loop=loop=-1:size=1:start=0,setpts=N/({fps}*TB)[halo];"
        f"[card][halo]overlay=x='240+30*sin(min(t,{hold:g})/30)':"
        f"y='450+20*sin(min(t,{hold:g})/30)':eval=frame:shortest=1"
    )
