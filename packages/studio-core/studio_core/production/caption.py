"""Source credits built from the same immutable scene and asset contract."""

from typing import Any

from .model import validate

# Owner 11 Nov 2026 («أعتمد كل مقطع»): a clip with machine-translated text reaches him with
# this notice, apart from the caption he copies, so nothing translated is posted unread.
TRANSLATED_NOTE = (
    "⚠️ هذا المقطع فيه نص مترجم آليًا من مصدر إنجليزي. راجع الترجمة قبل النشر؛ لا يُنشر إلا باعتمادك."
)


def machine_translated(value: Any) -> bool:
    """Whether any scene of this clip carries text translated from its source."""
    plan = value.get("legacy_production") if isinstance(value, dict) else None
    scenes = plan.get("scenes") if isinstance(plan, dict) else None
    return isinstance(scenes, list) and any(
        isinstance(scene, dict) and bool(scene.get("translations")) for scene in scenes
    )


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


def platform_caption(value: dict[str, Any]) -> str:
    """Keep original article links and required credits within the platform's limit."""
    p = validate(value)
    rows = [value["title"], "@rawwag_khalid"]
    for scene in p["scenes"]:
        if scene.get("source"):
            rows.append(scene["source"])
        if scene.get("source_url"):
            rows.append(scene["source_url"])
        if scene.get("image_credit"):
            rows.append("مصدر الصورة: " + scene["image_credit"])
    if p.get("music"):
        asset = next(a for a in p["assets"] if a["file"] == p["music"])
        rows.append("الموسيقى: " + asset["credit"])
    text = "\n".join(dict.fromkeys(rows))
    if len(text.encode("utf-16-le")) // 2 > 1024:
        raise ValueError("PRODUCTION_CAPTION_TOO_LONG")
    return text
