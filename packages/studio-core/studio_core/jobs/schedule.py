"""The old bots' schedule, in V3 (owner 21 Nov 2026: «احتاج الجدوله نفس البوت السابق»).

Snap (old snap.py SLOTS): fixed daily times, each due for 90 minutes, plus the breaking-news
radar (at most six a day, 06:00-24:00). WhatsApp (old status-bot run.due): prayer times in
Riyadh (Umm al-Qura), each due from adhan+15 to adhan+75 minutes, plus fixed times. Old kinds
map to today's commands. Each bot pauses its own schedule (a flag in its state folder).
"""

import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

RIYADH = ZoneInfo("Asia/Riyadh")
LATITUDE, LONGITUDE = 24.7136, 46.6753  # Riyadh
PAUSED = "SCHEDULE_PAUSED"

# Old snap.py SLOTS; «tadabbur» is today's «آية اليوم», «adhkar» the morning adhkar. The 23:00
# text report is now the supervisor's morning report.
SNAP_SLOTS: tuple[tuple[str, str], ...] = (
    ("06:45", "adhkar_am"),
    ("07:30", "hadith"),
    ("08:00", "verse"),
    ("09:00", "facts"),
    ("10:00", "tech"),
    ("12:00", "style"),
    ("13:00", "funny"),
    ("14:00", "riddle"),
    ("16:00", "tech"),
    ("17:00", "facts"),
    ("18:00", "style"),
    ("19:00", "funny"),
    ("20:00", "tech"),
    ("21:00", "style"),
    ("22:00", "night"),
)
SNAP_WINDOW = timedelta(minutes=90)
RADAR = "urgent"
RADAR_CAP = 6
RADAR_HOURS = (6, 24)

# Old status-bot run.due fixed times.
STATUS_FIXED: tuple[tuple[str, str], ...] = (
    ("07:00", "hadith"),
    ("07:30", "brief"),
    ("13:30", "news_noon"),
    ("21:00", "news"),
    ("22:00", "night"),
)
STATUS_WINDOW = timedelta(minutes=60)
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
    asr_alt = math.degrees(
        math.atan(1 / (1 + math.tan(math.radians(abs(LATITUDE - decl)))))
    )
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
        for clock, command in SNAP_SLOTS:
            at = _at(day, clock)
            key = f"{stamp}-{clock}-{command}"
            if at <= now < at + SNAP_WINDOW and key not in done:
                found.append((key, command))
    elif app == "status":
        for prayer, adhan in prayer_times(day).items():
            if adhan + timedelta(minutes=AFTER) <= now < adhan + timedelta(minutes=UNTIL):
                key = f"{stamp}-{prayer}"
                if key not in done:
                    found.append((key, prayer_command(prayer, day)))
        for clock, command in STATUS_FIXED:
            at = _at(day, clock)
            key = f"{stamp}-{command}"
            if at <= now < at + STATUS_WINDOW and key not in done:
                found.append((key, command))
    else:
        raise ValueError("UNKNOWN_APP")
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


def _clock(value: datetime) -> str:
    text = value.astimezone(RIYADH).strftime("%-I:%M")
    return text.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")) + (
        " ص" if value.astimezone(RIYADH).hour < 12 else " م"
    )


def schedule_text(app: str, now: datetime, root: Path) -> str:
    """«مواعيد الجدولة» for one bot, today."""
    from studio_core.commands import BY_KEY

    day = now.astimezone(RIYADH).date()
    rows = []
    if app == "snap":
        rows = [(_at(day, c), BY_KEY[k].label) for c, k in SNAP_SLOTS]
        tail = f"\n🚨 رادار العاجل: حتى {RADAR_CAP} مقاطع يوميًا من ٦ ص إلى ١٢ ل، إذا فيه خبر جديد."
    else:
        rows = [
            (adhan + timedelta(minutes=AFTER), BY_KEY[prayer_command(p, day)].label + f" (بعد {PRAYER_NAMES[p]})")
            for p, adhan in prayer_times(day).items()
        ]
        rows += [(_at(day, c), BY_KEY[k].label) for c, k in STATUS_FIXED]
        tail = "\nواتساب يرسل فقط إذا راسلت البوت خلال آخر ٢٤ ساعة."
    rows.sort()
    state = "⏸ موقوفة — اكتب «تشغيل الجدولة»" if paused(root) else "▶️ شغالة"
    body = "\n".join(f"{_clock(at)} — {label}" for at, label in rows)
    return f"🗓 مواعيد الجدولة اليوم ({state})\n{body}{tail}"
