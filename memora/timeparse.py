"""Deterministic parser for Uzbek / Russian / English relative time expressions.

Used on the fast path (e.g. the "✏️ Vaqtni o'zgartirish" flow) so common phrases such as
``ertaga 15:00``, ``indinga kechqurun``, ``juma kuni soat 3 da`` or ``20 daqiqadan keyin``
never need an LLM round-trip. Anything it cannot resolve returns ``None`` and the caller
falls back to the LLM.

Cyrillic input (Uzbek or Russian) is transliterated to Latin first, so a single set of
Latin patterns covers ``эртага``, ``завтра`` and ``ertaga`` alike.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from memora import timeutil

_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh",
    "ъ": "'", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya", "ў": "o'", "қ": "q", "ғ": "g'", "ҳ": "h",
}


def translit(text: str) -> str:
    """Lower-case and map Cyrillic (Uzbek + Russian) letters onto Uzbek Latin."""
    text = text.lower().replace("ʻ", "'").replace("ʼ", "'").replace("’", "'").replace("`", "'").replace("‘", "'")
    return "".join(_CYR.get(ch, ch) for ch in text)


_NUM_WORDS = {
    "bir": 1, "ikki": 2, "uch": 3, "to'rt": 4, "besh": 5, "olti": 6, "yetti": 7, "sakkiz": 8,
    "to'qqiz": 9, "o'n": 10, "o'n bir": 11, "o'n ikki": 12, "o'n besh": 15, "yigirma": 20,
    "o'ttiz": 30, "qirq": 40, "ellik": 50,
    "odin": 1, "odnu": 1, "dva": 2, "dve": 2, "tri": 3, "chetire": 4, "pyat": 5, "desyat": 10,
    "pyatnadtsat": 15, "dvadtsat": 20, "tridtsat": 30, "sorok": 40,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10, "fifteen": 15, "twenty": 20,
    "thirty": 30, "an": 1, "a": 1,
}
_NUM = r"(\d{1,3}|" + "|".join(sorted((re.escape(k) for k in _NUM_WORDS), key=len, reverse=True)) + r")"

_WEEKDAYS = [
    ("dushanba", "ponedelnik", "monday", "mon"),
    ("seshanba", "vtornik", "tuesday", "tue"),
    ("chorshanba", "sreda", "sredu", "wednesday", "wed"),
    ("payshanba", "chetverg", "thursday", "thu"),
    ("juma", "pyatnitsa", "pyatnitsu", "friday", "fri"),
    ("shanba", "subbota", "subbotu", "saturday", "sat"),
    ("yakshanba", "voskresene", "sunday", "sun"),
]
_MONTHS = [
    ("yanvar", "yanvarya", "january", "jan"), ("fevral", "fevralya", "february", "feb"),
    ("mart", "marta", "march", "mar"), ("aprel", "aprelya", "april", "apr"), ("may", "maya"),
    ("iyun", "iyunya", "june", "jun"), ("iyul", "iyulya", "july", "jul"),
    ("avgust", "avgusta", "august", "aug"), ("sentabr", "sentyabr", "sentyabrya", "september", "sep"),
    ("oktabr", "oktyabr", "oktyabrya", "october", "oct"), ("noyabr", "noyabrya", "november", "nov"),
    ("dekabr", "dekabrya", "december", "dec"),
]
_MONTH_ALT = "|".join(sorted((n for names in _MONTHS for n in names), key=len, reverse=True))

_DAY_WORDS = {
    0: ("bugun", "segodnya", "today", "hozir"),
    1: ("ertaga", "zavtra", "tomorrow"),
    2: ("indinga", "indin", "poslezavtra", "day after tomorrow"),
}
# part of day -> (default time, is_pm)
_PARTS = [
    (("tushdan keyin", "tushdan so'ng", "posle obeda", "dnem", "afternoon"), time(15, 0), True),
    (("ertalab", "erta tong", "tongda", "utrom", "utra", "morning"), time(8, 0), False),
    (("kechqurun", "kechki", "kechda", "oqshom", "vecherom", "vechera", "evening"), time(19, 0), True),
    (("kechasi", "tunda", "nochyu", "tonight", "night"), time(21, 0), True),
    (("tushda", "tushlik", "peshin", "v obed", "noon"), time(13, 0), True),
]

_REL_RE = re.compile(
    rf"(?:cherez\s+|in\s+)?{_NUM}\s*(daqiqa|minut|min|soat|chas|hour|hr|kun|den|dnya|dney|day|hafta|nedel|week)\w*"
    r"(?:\s*(?:dan)?\s*(?:keyin|so'ng|kechroq|later))?"
)
_REL_TRIGGER = re.compile(r"keyin|so'ng|cherez|\bin\b|later|dan\b|kechroq")
_HALF_HOUR_RE = re.compile(r"yarim\s+soat|polchasa|half\s+an\s+hour")
_CLOCK_RE = re.compile(r"(?<![\d.])([01]?\d|2[0-3])[:.]([0-5]\d)(?![\d.])")
_HOUR_RE = re.compile(
    rf"(?:soat\s+|v\s+|at\s+){_NUM}(\s+yarim)?(?:\s*(?:da|ga|gacha|chasov|chasa|chas))?(?:\s*(am|pm))?"
    rf"|{_NUM}(\s+yarim)?\s*(?:da|larda)\b"
    r"|(\d{1,2})\s*(am|pm)"
)
_DATE_ISO_RE = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")
_DATE_DMY_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](20\d{2}))?(?!\d)(?!\s*(?:da\b|ga\b))")
_DATE_NAMED_RE = re.compile(rf"(\d{{1,2}})\s*[- ]?\s*({_MONTH_ALT})\b")


@dataclass(frozen=True, slots=True)
class ParsedTime:
    when: datetime
    has_date: bool
    has_time: bool


def _num(token: str) -> int:
    return int(token) if token.isdigit() else _NUM_WORDS[token]


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_when(text: str, base: datetime | None = None) -> ParsedTime | None:
    """Resolve ``text`` to a concrete local datetime, or None when nothing was recognised."""
    base = timeutil.local(base) if base else timeutil.now()
    s = " " + re.sub(r"\s+", " ", translit(text)) + " "

    # 1) Pure offsets: "20 daqiqadan keyin", "через 2 часа", "in 3 days", "yarim soatdan keyin".
    if _HALF_HOUR_RE.search(s) and _REL_TRIGGER.search(s):
        return ParsedTime(base + timedelta(minutes=30), True, True)
    rel = _REL_RE.search(s)
    if rel and _REL_TRIGGER.search(rel.group(0) + s[rel.end(): rel.end() + 12] + s[max(0, rel.start() - 8): rel.start()]):
        amount, unit = _num(rel.group(1)), rel.group(2)
        if unit in ("daqiqa", "minut", "min"):
            return ParsedTime(base + timedelta(minutes=amount), True, True)
        if unit in ("soat", "chas", "hour", "hr"):
            return ParsedTime(base + timedelta(hours=amount), True, True)
        if unit in ("kun", "den", "dnya", "dney", "day"):
            s = s.replace(rel.group(0), " ")
            day: date | None = base.date() + timedelta(days=amount)
        else:
            s = s.replace(rel.group(0), " ")
            day = base.date() + timedelta(weeks=amount)
    else:
        day = None

    # 2) Calendar date.
    if day is None:
        if m := _DATE_ISO_RE.search(s):
            day = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            s = s.replace(m.group(0), " ")
        elif m := _DATE_NAMED_RE.search(s):
            month = next(i for i, names in enumerate(_MONTHS, 1) if m.group(2) in names)
            day = _safe_date(base.year, month, int(m.group(1)))
            if day and day < base.date():
                day = _safe_date(base.year + 1, month, int(m.group(1)))
            s = s.replace(m.group(0), " ")
        elif m := _DATE_DMY_RE.search(s):
            # "25.11" is a date; "15.30" (month 30) or "soat 10.05" is a clock time.
            d_, mo_ = int(m.group(1)), int(m.group(2))
            after_clock_word = re.search(r"(soat|\bv|\bat)\s*$", s[: m.start()]) is not None
            if 1 <= mo_ <= 12 and 1 <= d_ <= 31 and not after_clock_word:
                year = int(m.group(3)) if m.group(3) else base.year
                day = _safe_date(year, mo_, d_)
                if day and not m.group(3) and day < base.date():
                    day = _safe_date(year + 1, mo_, d_)
                if day:
                    s = s.replace(m.group(0), " ")
    if day is None:
        for offset, words in _DAY_WORDS.items():
            if any(re.search(rf"\b{re.escape(w)}\b", s) for w in words):
                day = base.date() + timedelta(days=offset)
                break
    if day is None:
        # Longest names first so "yakshanba"/"dushanba" are not read as "shanba".
        flat = sorted(((n, idx) for idx, names in enumerate(_WEEKDAYS) for n in names), key=lambda x: -len(x[0]))
        for name, idx in flat:
            if re.search(rf"\b{name}", s):
                ahead = (idx - base.weekday()) % 7
                if re.search(r"keyingi|kelasi|sleduyush|next", s) and ahead == 0:
                    ahead = 7
                day = base.date() + timedelta(days=ahead)
                weekday_named = True
                break
        else:
            weekday_named = False
    else:
        weekday_named = False

    # 3) Clock time.
    clock: time | None = None
    part_default: time | None = None
    is_pm = False
    is_am = False
    for words, default, pm in _PARTS:
        if any(re.search(rf"\b{re.escape(w)}", s) for w in words):
            part_default, is_pm, is_am = default, pm, not pm
            break
    if m := _CLOCK_RE.search(s):
        clock = time(int(m.group(1)), int(m.group(2)))
    if clock is None:
        m = _HOUR_RE.search(s)
        if m:
            token = m.group(1) or m.group(4) or m.group(6)
            half = bool(m.group(2) or m.group(5))
            meridiem = m.group(3) or m.group(7)
            hour = _num(token)
            if 0 <= hour <= 24:
                clock = time(hour % 24, 30 if half else 0)
                if meridiem == "pm":
                    is_pm = True
                elif meridiem == "am":
                    is_am = True
    if clock is not None and clock.hour <= 12:
        if is_pm and clock.hour < 12:
            clock = clock.replace(hour=clock.hour + 12)
        elif is_am and clock.hour == 12:
            clock = clock.replace(hour=0)
        elif not is_am and not is_pm and 1 <= clock.hour <= 7 and _CLOCK_RE.search(s) is None:
            # "soat 3 da" from a student almost always means 15:00.
            clock = clock.replace(hour=clock.hour + 12)
    if clock is None:
        clock = part_default

    if day is None and clock is None:
        return None
    has_date, has_time = day is not None, clock is not None
    if day is None:
        day = base.date()
        if timeutil.combine(day, clock) <= base:  # time already passed today -> tomorrow
            day += timedelta(days=1)
    when = timeutil.combine(day, clock or time(0, 0))
    if weekday_named and has_time and when <= base:
        when += timedelta(days=7)
    return ParsedTime(when, has_date, has_time)
