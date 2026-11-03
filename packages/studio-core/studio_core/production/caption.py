"""Source credits built from the same immutable scene and asset contract."""

from typing import Any

from .model import validate


def caption(value: dict[str, Any]) -> str:
    p = validate(value)
    rows = [value["title"], "@rawwag_khalid"]
    for scene in p["scenes"]:
        if scene.get("source"):
            rows.append(scene["source"])
        if scene.get("source_url"):
            rows.append(scene["source_url"])
        if scene.get("image"):
            asset = next(a for a in p["assets"] if a["file"] == scene["image"])
            rows.append("مصدر الصورة: " + scene["image_credit"] + " — " + asset["source_url"])
        if scene.get("verse"):
            s, a = scene["verse"]
            rows.append(
                f"التفسير الميسّر: https://quranenc.com/api/v1/translation/aya/arabic_moyassar/{s}/{a}"
            )
    if p.get("music"):
        asset = next(a for a in p["assets"] if a["file"] == p["music"])
        rows.append("الموسيقى: " + asset["credit"])
    for asset in p["assets"]:
        if asset["file"] in {a["file"] for a in p["audio"]}:
            rows.append("الصوت: " + asset["credit"] + " — " + asset["source_url"])
    return "\n".join(dict.fromkeys(rows))
