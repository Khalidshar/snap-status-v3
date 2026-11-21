"""The daily posting plan (owner 21 Nov 2026: «برمجه» — the plan for 3,000 USD a month).

Snap: nine posts a day aimed at Riyadh's evening peak (8 pm-1 am): Stories for news, adhkar
and the bedtime series (where Snap's mid-roll ads live), Spotlight for original formats
(facts, riddles, funny, style, tech) in the peak hours, and the breaking-news radar (at most
three a day). WhatsApp feeds Snap followers: three prayer-time clips (Umm al-Qura, Riyadh) and
two evening ones. Every clip is made LEAD minutes before its posting time so Khalid can post on
time. Each bot pauses its own schedule (a flag in its state folder).
"""

import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

RIYADH = ZoneInfo("Asia/Riyadh")
LATITUDE, LONGITUDE = 24.7136, 46.6753  # Riyadh
PAUSED = "SCHEDULE_PAUSED"
LEAD = timedelta(minutes=30)  # made this long before its posting time
WINDOW = timedelta(minutes=60)  # still made if the server catches up within an hour
STORY, SPOTLIGHT = "القصص", "الأضواء"


def snap_slots(day: date) -> list[tuple[str, str, str]]:
    """(posting time, command, where) for one day. Spotlight kinds rotate daily and never
    repeat within a day; news never goes to Spotlight (Snap policy)."""
    n = day.toordinal()
    peak = ("riddle", "funny")[n % 2]
    late = "funny" if peak == "riddle" else "riddle"
    return [
        ("06:45", "adhkar_am", STORY),
        ("07:30", "saudi", STORY),
        ("13:00", "news_noon", STORY),
        ("17:30", "facts", SPOTLIGHT),
        ("20:00", ("world", "sports")[n % 2], STORY),
        ("21:00", peak, SPOTLIGHT),
        ("22:00", "night", STORY),
        ("22:30", ("style", "tech")[n % 2], SPOTLIGHT),
        ("23:30", late, SPOTLIGHT),
    ]


RADAR = "urgent"
RADAR_CAP = 3
RADAR_HOURS = (6, 24)

STATUS_PRAYERS = ("fajr", "dhuhr", "maghrib")
STATUS_FIXED: tuple[tuple[str, str], ...] = (("21:00", "news"), ("22:00", "night"))
AFTER, UNTIL = 15, 75  # minutes after the adhan
PRAYER_NAMES = {
    "fajr": "الفجر",
    "dhuhr": "الظهر",
    "asr": "العصر",
    "maghrib": "المغرب",
    "isha": "العشاء",
}


def prayer_command(prayer: str, day: date) -> str:
    """Old mapping. Friday after Fajr: رسالة الجمعة; Sunday and Wednesday evenings: the
    Monday/Thursday fast; Thursday evening: سورة الكهف; other evenings: آية اليوم."""
    weekday = day.weekday()  # Monday 0
    if prayer == "fajr":
        return "friday" if weekday == 4 else "adhkar_am"
    if prayer == "dhuhr":
        return "dua"
    if prayer == "asr":
        return "adhkar_pm"
    if prayer == "maghrib":
        return {6: "fast", 2: "fast", 3: "kahf"}.get(weekday, "verse")
    if prayer == "isha":
        return "isha"
    raise ValueError("UNKNOWN_PRAYER")


def _sun(day: date) -> tuple[float, float]:
    """Solar declination (degrees) and equation of time (minutes), NOAA approximation."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eot = 229.18 * (
        0.000075
        + 0.001868 * math.cos(g)
        - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g)
        - 0.040849 * math.sin(2 * g)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(g)
        + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g)
        + 0.000907 * math.sin(2 * g)
        - 0.002697 * math.cos(3 * g)
        + 0.00148 * math.sin(3 * g)
    )
    return math.degrees(decl), eot


def _hour_angle(altitude: float, decl: float) -> float:
    lat, d = math.radians(LATITUDE), math.radians(decl)
    value = (math.sin(math.radians(altitude)) - math.sin(lat) * math.sin(d)) / (
        math.cos(lat) * math.cos(d)
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, value)))) / 15  # hours


def prayer_times(day: date) -> dict[str, datetime]:
    """Riyadh prayer times by Umm al-Qura rules: Fajr at 18.5°, Asr at one shadow length,
    Isha 90 minutes after Maghrib (120 in Ramadan is not applied here)."""
    decl, eot = _sun(day)
    noon = 12 + 3 - LONGITUDE / 15 - eot / 60  # local solar noon in Riyadh time (UTC+3)
    asr_alt = math.degrees(math.atan(1 / (1 + math.tan(math.radians(abs(LATITUDE - decl))))))
    hours = {
        "fajr": noon - _hour_angle(-18.5, decl),
        "dhuhr": noon + 1 / 60,
        "asr": noon + _hour_angle(asr_alt, decl),
        "maghrib": noon + _hour_angle(-0.833, decl) + 1 / 60,
    }
    hours["isha"] = hours["maghrib"] + 1.5
    start = datetime.combine(day, time(0), RIYADH)
    return {name: start + timedelta(hours=value) for name, value in hours.items()}


def _at(day: date, clock: str) -> datetime:
    hour, minute = map(int, clock.split(":"))
    return datetime.combine(day, time(hour, minute), RIYADH)


def due(app: str, now: datetime, done: set[str]) -> list[tuple[str, str]]:
    """(key, command) pairs due now and not yet done. Keys are per day and slot."""
    now = now.astimezone(RIYADH)
    day = now.date()
    stamp = day.strftime("%Y%m%d")
    found: list[tuple[str, str]] = []
    if app == "snap":
        fixed = [(clock, command) for clock, command, _ in snap_slots(day)]
    elif app == "status":
        fixed = list(STATUS_FIXED)
        for prayer, adhan in prayer_times(day).items():
            if prayer not in STATUS_PRAYERS:
                continue
            if adhan + timedelta(minutes=AFTER) <= now < adhan + timedelta(minutes=UNTIL):
                key = f"{stamp}-{prayer}"
                if key not in done:
                    found.append((key, prayer_command(prayer, day)))
    else:
        raise ValueError("UNKNOWN_APP")
    for clock, command in fixed:
        post = _at(day, clock)
        key = f"{stamp}-{clock}-{command}"
        if post - LEAD <= now < post - LEAD + WINDOW and key not in done:
            found.append((key, command))
    return found


def radar_open(now: datetime, sent_today: int) -> bool:
    hour = now.astimezone(RIYADH).hour
    return RADAR_HOURS[0] <= hour < RADAR_HOURS[1] and sent_today < RADAR_CAP


def paused(root: Path) -> bool:
    return (root / PAUSED).exists()


def set_paused(root: Path, value: bool) -> None:
    flag = root / PAUSED
    if value:
        flag.write_text("paused by the owner\n", encoding="utf-8")
    elif flag.exists():
        flag.unlink()


def _digits(value: object) -> str:
    return str(value).translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))


def _clock(value: datetime) -> str:
    local = value.astimezone(RIYADH)
    text = f"{local.hour % 12 or 12}:{local.minute:02d}"
    return text.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")) + (
        " ص" if value.astimezone(RIYADH).hour < 12 else " م"
    )


def schedule_text(app: str, now: datetime, root: Path) -> str:
    """«مواعيد الجدولة» for one bot, today: posting times, and where on Snap."""
    from studio_core.commands import BY_KEY

    day = now.astimezone(RIYADH).date()
    if app == "snap":
        rows = [(_at(day, c), f"{BY_KEY[k].label} ({where})") for c, k, where in snap_slots(day)]
        tail = (
            f"\n🚨 العاجل: حتى {_digits(RADAR_CAP)} يوميًا من ٦ ص إلى ١٢ ل، إذا فيه خبر جديد."
            "\nكل مقطع يجيك قبل موعده بنص ساعة."
        )
    else:
        rows = [
            (
                adhan + timedelta(minutes=AFTER),
                BY_KEY[prayer_command(p, day)].label + f" (بعد {PRAYER_NAMES[p]})",
            )
            for p, adhan in prayer_times(day).items()
            if p in STATUS_PRAYERS
        ]
        rows += [(_at(day, c), BY_KEY[k].label) for c, k in STATUS_FIXED]
        tail = "\nواتساب يرسل فقط إذا راسلت البوت خلال آخر ٢٤ ساعة. هدفه يجيب متابعين لسناب."
    rows.sort()
    state = "⏸ موقوفة — اكتب «تشغيل الجدولة»" if paused(root) else "▶️ شغالة"
    body = "\n".join(f"{_clock(at)} — {label}" for at, label in rows)
    return f"🗓 مواعيد الجدولة اليوم ({state})\n{body}{tail}"
