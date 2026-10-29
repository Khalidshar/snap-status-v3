"""The owner's command menu: one list, identical for the Snap and Status bots.

Same names, same numbers, same sections on Telegram and WhatsApp. Seven sections keep
every list within WhatsApp's 10-row limit. Matching is exact: a message runs a command
only when it is that command's number, its name (with or without its emoji) or one of
its listed spellings. Any other text shows the menu and runs nothing.
Commands marked confirm need the owner to answer "نعم" within CONFIRM_SECONDS.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

CONFIRM_SECONDS = 120
YES = "نعم"


@dataclass(frozen=True)
class Command:
    number: int
    key: str
    label: str
    section: str
    aliases: tuple[str, ...] = ()
    confirm: bool = False


SECTIONS: tuple[tuple[str, str, tuple[tuple[str, str, tuple[str, ...], bool], ...]], ...] = (
    ("news", "📰 أخبار", (
        ("urgent", "🚨 خبر عاجل", ("عاجل",), False),
        ("saudi", "🇸🇦 أخبار السعودية", ("السعودية",), False),
        ("world", "🌍 أخبار العالم", ("العالم",), False),
        ("news", "📰 موجز الأخبار", ("موجز",), False),
        ("football", "⚽ ملخص كرة القدم", ("كرة القدم", "الكرة"), False),
        ("markets", "📈 الأسواق والاقتصاد", ("الأسواق",), False),
    )),
    ("general", "💡 هل تعلم ومنوعات", (
        ("facts", "💡 هل تعلم", (), False),
        ("tech", "📱 التقنية", ("تقنية",), False),
        ("style", "👗 الأناقة والجمال", ("الأناقة",), False),
        ("riddle", "🧠 لغز اليوم", ("لغز",), False),
        ("funny", "😂 مواقف طريفة", ("طرائف",), False),
        ("night", "🌙 قبل النوم", ("ليلة طيبة",), False),
    )),
    ("faith", "🕌 أذكار ودين", (
        ("adhkar_am", "🌅 أذكار الصباح", ("اذكار الصباح",), False),
        ("adhkar_pm", "🌆 أذكار المساء", ("اذكار المساء",), False),
        ("dua", "🤲 دعاء اليوم", ("دعاء",), False),
        ("hadith", "🕌 حديث اليوم", ("حديث",), False),
        ("verse", "📖 آية اليوم", ("اية اليوم", "آية"), False),
        ("friday", "🕌 رسالة الجمعة", ("الجمعة",), False),
        ("kahf", "📖 سورة الكهف", ("الكهف",), False),
        ("fast", "🌙 تذكير الصيام", ("صيام",), False),
    )),
    ("brief", "⏱️ يومك في دقيقة", (
        ("brief", "⏱️ يومك في دقيقة", ("يومك",), False),
    )),
    ("status", "📊 الحالة والتقارير", (
        ("status", "📡 الحالة", ("حالة الاستوديو",), False),
        ("report", "📊 تقرير اليوم", ("التقرير", "تقرير"), False),
        ("agents", "🤖 حالة الوكلاء", ("الوكلاء",), False),
        ("last", "📥 آخر مقطع", (), False),
        ("dash", "🖥️ لوحة التحكم", ("لوحة التحكم",), False),
    )),
    ("control", "⏯ التحكم", (
        ("preview", "👁 معاينة", (), False),
        ("send", "✅ أرسل", ("ارسل",), True),
        ("cancel", "❌ ألغِ", ("الغ", "إلغاء"), False),
        ("pause", "⏸ إيقاف الجدولة", (), False),
        ("resume", "▶️ تشغيل الجدولة", (), False),
        ("schedule", "🗓 مواعيد الجدولة", ("مواعيد",), False),
        ("halt", "🛑 إيقاف كامل", ("طوارئ", "أوقف"), True),
        ("unhalt", "🟢 رفع الإيقاف الكامل", ("شغّل",), True),
    )),
    ("help", "❓ مساعدة", (
        ("menu", "📋 القائمة", ("مساعدة", "الأوامر", "اوامر"), False),
        ("add_fact", "➕ أضف معلومة", (), False),  # noqa: RUF001  # the menu's emoji
    )),
)  # fmt: skip


def _build() -> tuple[Command, ...]:
    commands: list[Command] = []
    for section, _, rows in SECTIONS:
        for key, label, aliases, confirm in rows:
            commands.append(Command(len(commands) + 1, key, label, section, aliases, confirm))
    return tuple(commands)


COMMANDS = _build()
BY_KEY = {c.key: c for c in COMMANDS}
ADD_FACT_PREFIX = "أضف هل تعلم:"


def normalize(text: str) -> str:
    """Spelling only: digits, alef forms, taa marbuta, tatweel, marks, emoji, spaces."""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    text = re.sub("[ً-ٰٟـ]", "", text)
    text = re.sub("[أإآ]", "ا", text).replace("ة", "ه").replace("ى", "ي")
    text = "".join(ch for ch in text if ch.isalnum() or ch.isspace() or ch in ":")
    return " ".join(text.split())


def _index() -> dict[str, Command]:
    index: dict[str, Command] = {}
    for command in COMMANDS:
        for form in (str(command.number), command.label, *command.aliases):
            key = normalize(form)
            if key in index and index[key] is not command:
                raise ValueError("Ambiguous command spelling: " + form)
            index[key] = command
    for _, title, _ in SECTIONS:
        # A section title may only equal a command when that command is the section's own.
        same = index.get(normalize(title))
        if same and same.key not in ("brief", "menu"):
            raise ValueError("Section title collides with a command: " + title)
    return index


INDEX = _index()
SECTION_INDEX = {normalize(title): section for section, title, _ in SECTIONS}


def parse(text: Any) -> dict[str, Any]:
    """{"kind": "command"|"section"|"add_fact"|"yes"|"menu", ...}. Never guesses."""
    if not isinstance(text, str) or not text.strip() or len(text) > 600:
        return {"kind": "menu"}
    stripped = text.strip()
    if stripped.startswith(ADD_FACT_PREFIX):
        body = stripped[len(ADD_FACT_PREFIX) :].strip()
        return {"kind": "add_fact", "text": body} if body else {"kind": "menu"}
    key = normalize(stripped)
    if key == normalize(YES):
        return {"kind": "yes"}
    if key in ("0", "") or key in ("start", "help", "menu"):
        return {"kind": "menu"}
    command = INDEX.get(key)
    if command:
        if command.key == "menu":
            return {"kind": "menu"}
        return {"kind": "command", "command": command}
    section = SECTION_INDEX.get(key)
    if section:
        if section == "brief":
            return {"kind": "command", "command": BY_KEY["brief"]}
        return {"kind": "section", "section": section}
    return {"kind": "menu"}


def section_rows(section: str) -> list[Command]:
    return [c for c in COMMANDS if c.section == section]


def menu_text() -> str:
    lines = ["🤖 القائمة: اكتب رقم الأمر أو اسمه، أو اسم القسم لعرضه.", ""]
    for section, title, _ in SECTIONS:
        rows = section_rows(section)
        span = str(rows[0].number)
        if len(rows) > 1:
            span += "–" + str(rows[-1].number)  # noqa: RUF001  # Arabic text uses the en dash
        lines.append(title + " (" + span + ")")
    lines += ["", "أي رسالة أخرى تعرض هذه القائمة ولا تنفّذ شيئًا."]
    return "\n".join(lines)


def section_text(section: str) -> str:
    title = next(t for s, t, _ in SECTIONS if s == section)
    rows = [
        str(c.number) + ". " + c.label + (" (يحتاج نعم)" if c.confirm else "")
        for c in section_rows(section)
    ]
    return "\n".join([title, "", *rows])


def confirm_text(command: Command) -> str:
    return command.label + ": أرسل «" + YES + "» خلال دقيقتين للتأكيد، وأي شيء آخر يلغيه."


def whatsapp_list(section: str | None = None) -> dict[str, Any]:
    """WhatsApp interactive list: at most 10 rows, titles at most 24 characters."""
    if section is None:
        rows = [{"id": "s:" + s, "title": t[:24]} for s, t, _ in SECTIONS]
    else:
        rows = [{"id": "c:" + str(c.number), "title": c.label[:24]} for c in section_rows(section)]
    if len(rows) > 10:
        raise ValueError("WhatsApp lists allow at most 10 rows")
    return {"button": "القائمة", "sections": [{"title": "الأوامر", "rows": rows}]}
