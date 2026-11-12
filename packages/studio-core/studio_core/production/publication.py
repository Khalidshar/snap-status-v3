"""Owner's publication destination; advisory metadata, never automatic publishing."""

import re
from typing import Any

LABELS = {"PUBLIC_STORY": "القصص العامة", "SPOTLIGHT": "الأضواء", "UNKNOWN": "لم تُحدد"}
POLITICAL = re.compile(
    r"(?<![ء-ي])[وفبكل]{0,2}(?:ال)?(?:"
    r"رئيس (?:الوزراء|الحكومة|الدولة|الجمهورية)|رئيس وزراء|ولي العهد|ملك|حكوم[ةه]|"
    r"انتخاب(?:ات)?|برلمان|سياسي(?:[ةه]|ون|ين)?|دبلوماسي(?:[ةه]|ون|ين)?|"
    r"مجلس الامن|سفير|سفاره|حرب|حروب|جيش|جيوش|عسكري(?:[ةه]|ون|ين)?|"
    r"قصف|غار[ةه]|غارات|صاروخ|صواريخ|غزو|اشتباك(?:ات)?|معارك|"
    r"وقف اطلاق النار|ترامب|بوتين|نتنياهو|زيلينسكي)(?![ء-ي])"
)
PRESS_DETENTION = re.compile(
    r"(?<![ء-ي])[وفبكل]{0,2}(?:ال)?(?:سجن|اعتقال|حبس|محاكمه|احتجاز|افراج عن) "
    r"(?:ال)?صح(?:افي|في)(?:ه|ين|ون)?(?![ء-ي])"
)


def target(value: Any) -> str:
    if not isinstance(value, dict):
        return "UNKNOWN"
    from .model import SACRED

    plan = value.get("legacy_production")
    plan = plan if isinstance(plan, dict) else {}
    key = plan.get("key") or value.get("kind")
    key = key if isinstance(key, str) else ""
    if key in SACRED:
        return "SPOTLIGHT"
    if key == "urgent":
        return "PUBLIC_STORY"
    fields = [value.get("title"), value.get("body")]
    scenes = plan.get("scenes", [])
    if isinstance(scenes, list):
        for scene in scenes:
            if isinstance(scene, dict):
                fields.extend(scene.get(k) for k in ("head", "body"))
    text = " ".join(field for field in fields if isinstance(field, str))
    if not text.strip():
        return "UNKNOWN"
    text = re.sub("[ً-ْـ]", "", text).translate(str.maketrans("أإآىة", "ااايه"))
    text = " ".join(text.split())
    for phrase in ("حرب الاسعار", "حرب النجوم", "ملك الغابه"):
        text = text.replace(phrase, " ")
    return "PUBLIC_STORY" if POLITICAL.search(text) or PRESS_DETENTION.search(text) else "SPOTLIGHT"


def notice(value: Any) -> str:
    return "وجهة النشر: " + LABELS[target(value)]
