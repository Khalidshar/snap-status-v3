"""Source credits built from the same immutable scene and asset contract."""

import re
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
    # The publication destination is for the owner only, never in the caption he posts
    # (review 13 Nov 2026); both bots send it to him as its own message.
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


# Owner 21 Nov 2026: «الروابط جميعها ما احتاجها؛ كل الي احتاجه نوع المقطع وعنوان موجز
# للمقطع ومتى ينشر واين ينشر». Riyadh time; advisory only, nothing is scheduled.
WHEN = {
    "urgent": "الآن فورًا",
    "saudi": "الآن، خلال ساعة",
    "world": "الآن، خلال ساعة",
    "news": "الآن، خلال ساعة",
    "markets": "الآن، خلال ساعة",
    "football": "الآن، خلال ساعة",
    "sports": "الآن، خلال ساعة",
    "news_noon": "الظهر من ١٢ إلى ١",
    "adhkar_am": "بعد الفجر حتى ٩ ص",
    "adhkar_pm": "بعد العصر حتى المغرب",
    "friday": "يوم الجمعة الصبح",
    "kahf": "يوم الجمعة الصبح",
    "fast": "مساء الأحد أو الأربعاء",
    "isha": "الليل من ١٠ إلى ١١",
    "night": "الليل من ١٠ إلى ١١",
    "brief": "الصبح من ٧ إلى ٩",
}
WHEN_DEFAULT = "المساء من ٨ إلى ١٠"
HEADLINE_MAX = 80


def headline(value: dict[str, Any], plan: dict[str, Any]) -> str:
    """The clip's first scene headline, cut at a word; else its type."""
    for scene in plan.get("scenes", []):
        head = scene.get("head") if isinstance(scene, dict) else None
        if isinstance(head, str) and head.strip():
            text = " ".join(head.split())
            if len(text) > HEADLINE_MAX:
                text = text[:HEADLINE_MAX].rsplit(" ", 1)[0] + "…"
            return text
    return str(value["title"])


def platform_caption(value: dict[str, Any]) -> str:
    """The owner's posting card: type, short headline, when and where. No links."""
    from .publication import LABELS

    p = validate(value)
    key = str(p.get("key") or value.get("kind") or "")
    rows = [
        "🎬 النوع: " + str(value["title"]),
        "📝 العنوان: " + headline(value, p),
        "🕒 متى ينشر: " + WHEN.get(key, WHEN_DEFAULT),
        "📍 وين ينشر: " + LABELS.get(str(value.get("publication_target")), LABELS["UNKNOWN"]),
    ]
    if p.get("music"):
        # CC BY music is licensed on condition of credit; the credit stays (no link).
        asset = next(a for a in p["assets"] if a["file"] == p["music"])
        bare = re.sub(r"https?://\S+", "", str(asset["credit"]))
        parts = [x.strip(" —-") for x in re.split(r"[\n|]", bare)]
        credit = " • ".join(x for x in parts if x and not x.lower().endswith(" by"))
        rows.append("🎵 الموسيقى: " + credit)
    text = "\n".join(rows)
    if len(text.encode("utf-16-le")) // 2 > 1024:
        raise ValueError("PRODUCTION_CAPTION_TOO_LONG")
    return text
