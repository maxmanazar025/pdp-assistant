"""Timezone helpers and Uzbek (Latin) date/time formatting."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from memora.config import get_settings

WEEKDAYS_UZ = ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"]
WEEKDAYS_EN = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS_UZ = [
    "yanvar", "fevral", "mart", "aprel", "may", "iyun",
    "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr",
]


def tz() -> ZoneInfo:
    return get_settings().tz


def now() -> datetime:
    """Timezone-aware 'now' in the user's zone (Asia/Tashkent by default)."""
    return datetime.now(tz())


def today() -> date:
    return now().date()


def local(dt: datetime) -> datetime:
    """Convert any aware datetime to the user's zone; treat naive values as local."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz())
    return dt.astimezone(tz())


def combine(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=tz())


def iso(dt: datetime) -> str:
    """ISO-8601 with the +05:00 offset, second precision."""
    return local(dt).isoformat(timespec="seconds")


def parse_iso_local(value: str) -> datetime:
    """Parse an ISO string; naive values are interpreted in the user's zone."""
    cleaned = value.strip().replace("Z", "+00:00")
    if len(cleaned) == 10:  # date only
        return combine(date.fromisoformat(cleaned), time(0, 0))
    return local(datetime.fromisoformat(cleaned))


def fmt_time(value: datetime | time) -> str:
    if isinstance(value, datetime):
        value = local(value).time()
    return value.strftime("%H:%M")


def fmt_date(d: date, with_weekday: bool = True) -> str:
    text = f"{d.day}-{MONTHS_UZ[d.month - 1]}"
    return f"{WEEKDAYS_UZ[d.weekday()]}, {text}" if with_weekday else text


def fmt_day_label(d: date, ref: date | None = None) -> str:
    """'bugun', 'ertaga', 'indinga' or 'juma, 20-noyabr'."""
    ref = ref or today()
    delta = (d - ref).days
    if delta == 0:
        return "bugun"
    if delta == 1:
        return "ertaga"
    if delta == 2:
        return "indinga"
    if delta == -1:
        return "kecha"
    return fmt_date(d)


def fmt_dt(dt: datetime, ref: date | None = None) -> str:
    """Human Uzbek datetime: 'ertaga 15:00' / 'juma, 20-noyabr 09:30'."""
    dt = local(dt)
    return f"{fmt_day_label(dt.date(), ref)} {fmt_time(dt)}"


def fmt_duration(minutes: int) -> str:
    hours, mins = divmod(max(int(minutes), 0), 60)
    if hours and mins:
        return f"{hours} soat {mins} daqiqa"
    if hours:
        return f"{hours} soat"
    return f"{mins} daqiqa"


def week_bounds(d: date) -> tuple[date, date]:
    """Monday..Sunday containing ``d``."""
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def day_bounds(d: date) -> tuple[datetime, datetime]:
    start = combine(d, time(0, 0))
    return start, start + timedelta(days=1)


def calendar_table(days: int = 10) -> str:
    """Compact date table injected into LLM prompts so relative dates resolve exactly."""
    base = today()
    rows = []
    for offset in range(days):
        d = base + timedelta(days=offset)
        label = {0: " (bugun)", 1: " (ertaga)", 2: " (indinga)"}.get(offset, "")
        rows.append(f"{d.isoformat()} = {WEEKDAYS_UZ[d.weekday()]}{label}")
    return "\n".join(rows)
