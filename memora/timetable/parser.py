"""Parser for the university's 2D merged timetable matrix (Google Sheets CSV export).

Observed layout of the real export
----------------------------------
* A header row with weekday names, each merged across N columns. Merged cells export as
  the value in the first column followed by blanks, so days must be forward-filled.
* The next row holds the time slots (``1. 08:00 - 09:20`` ...). The number of slots per
  day differs between tabs (8 on the degree tab, 7 on the English tab), so nothing is
  hard-coded.
* Each group owns a *block* of rows: the group name sits in column A of the first row
  only (vertical merge -> forward-fill). The rows of the block carry, per slot column:
  subject / teacher / room / shared group codes.
* Some exports instead put everything in one multi-line cell. Both shapes are handled by
  flattening "all lines of all rows of the block" per column: line 1 is the subject and
  every other line is classified as room, shared-group codes or teacher.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import asdict, dataclass
from datetime import time

from memora.timeutil import WEEKDAYS_EN

_SLOT_RE = re.compile(r"^\s*(?:(\d+)\s*[.)]\s*)?(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})\s*$")
_ROOM_RE = re.compile(r"\b(room|lab|laboratory|auditorium|aud\.?|hall|xona|zal|online|lecture room)\b", re.I)
_ROOM_NUM_RE = re.compile(r"^\s*\d{2,4}[A-Za-z]?\s*$")
_GROUP_CODE_RE = re.compile(r"\b\d{2}-\d{3}\b")
_KIND_RE = re.compile(r"\s*\((lecture|practice|lab|seminar)\)\s*$", re.I)
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ParsedEntry:
    group_key: str          # configured target, e.g. "25-408"
    group_raw: str          # as written in the sheet, e.g. "25-408 (UZ)"
    weekday: int            # 0 = Monday
    slot_no: int
    start_time: time
    end_time: time
    subject: str
    kind: str | None        # lecture | practice | lab | seminar
    course_key: str         # normalised subject without the (Lecture)/(Practice) suffix
    course_title: str
    room: str | None
    teacher: str | None
    shared_groups: str | None
    source: str

    @property
    def natural_key(self) -> tuple[str, int, time]:
        return (self.group_key, self.weekday, self.start_time)

    @property
    def content_hash(self) -> str:
        payload = "|".join(
            str(x or "")
            for x in (self.subject, self.room, self.teacher, self.shared_groups, self.end_time, self.slot_no)
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def as_dict(self) -> dict:
        data = asdict(self)
        data["start_time"] = self.start_time.strftime("%H:%M")
        data["end_time"] = self.end_time.strftime("%H:%M")
        return data


class TimetableParseError(ValueError):
    """Raised when the sheet no longer looks like a timetable matrix."""


def _clean(value: str | None) -> str:
    return _WS_RE.sub(" ", (value or "").replace("\xa0", " ")).strip()


def split_subject(subject: str) -> tuple[str, str | None]:
    """'Programming (Python) Level 4 (Practice)' -> ('Programming (Python) Level 4', 'practice')."""
    match = _KIND_RE.search(subject)
    if not match:
        return subject, None
    return subject[: match.start()].strip(), match.group(1).lower()


def course_key(title: str) -> str:
    return _WS_RE.sub(" ", title).strip().casefold()


def match_group(cell: str, targets: list[str]) -> str | None:
    """Return the configured target a column-A cell belongs to.

    '25-408 (UZ)' matches target '25-408'; 'Intermediate 13' must not match 'Intermediate 130'
    and 'Intermediate 1' must not match 'Intermediate 13'.
    """
    value = _clean(cell).casefold()
    for target in targets:
        t = _clean(target).casefold()
        if value == t:
            return target
        if value.startswith(t) and not value[len(t)].isalnum():
            return target
    return None


def _find_header(rows: list[list[str]]) -> tuple[int, int]:
    """Locate (day_row_index, slot_row_index)."""
    for idx, row in enumerate(rows[:15]):
        days = sum(1 for cell in row if _clean(cell).casefold() in WEEKDAYS_EN)
        if days >= 3:
            for j in range(idx + 1, min(idx + 4, len(rows))):
                if sum(1 for cell in rows[j] if _SLOT_RE.match(cell or "")) >= 3:
                    return idx, j
    raise TimetableParseError("weekday/time-slot header rows not found")


def _column_map(day_row: list[str], slot_row: list[str]) -> dict[int, tuple[int, int, time, time]]:
    """column index -> (weekday, slot_no, start, end), forward-filling the merged day cells."""
    mapping: dict[int, tuple[int, int, time, time]] = {}
    current_day: int | None = None
    slot_counter = 0
    width = max(len(day_row), len(slot_row))
    for col in range(1, width):
        day_cell = _clean(day_row[col] if col < len(day_row) else "").casefold()
        if day_cell in WEEKDAYS_EN:
            current_day = WEEKDAYS_EN.index(day_cell)
            slot_counter = 0
        match = _SLOT_RE.match(slot_row[col] if col < len(slot_row) else "")
        if current_day is None or not match:
            continue
        slot_counter += 1
        number, h1, m1, h2, m2 = match.groups()
        mapping[col] = (
            current_day,
            int(number) if number else slot_counter,
            time(int(h1), int(m1)),
            time(int(h2), int(m2)),
        )
    if not mapping:
        raise TimetableParseError("no time-slot columns recognised")
    return mapping


def _classify(lines: list[str]) -> tuple[str | None, str | None, str | None]:
    """Split the non-subject lines of a cell into (room, teacher, shared_groups)."""
    rooms: list[str] = []
    teachers: list[str] = []
    groups: list[str] = []
    for line in lines:
        if _GROUP_CODE_RE.search(line):
            groups.append(line)
        elif _ROOM_RE.search(line) or _ROOM_NUM_RE.match(line):
            rooms.append(line)
        else:
            teachers.append(line)
    join = lambda items: ", ".join(dict.fromkeys(items)) or None  # noqa: E731 - dedupe, keep order
    return join(rooms), join(teachers), join(groups)


def parse_timetable_csv(csv_text: str, targets: list[str], source: str) -> tuple[list[ParsedEntry], set[str]]:
    """Parse one sheet tab.

    Returns ``(entries, groups_found)`` where ``groups_found`` is the subset of ``targets``
    whose block exists in this tab (even if the block is currently empty) — the sync layer
    uses it to tell "group has no classes" apart from "group vanished from the sheet".
    """
    rows = [list(r) for r in csv.reader(io.StringIO(csv_text.lstrip("﻿")))]
    if len(rows) < 3:
        raise TimetableParseError("sheet has fewer than 3 rows")
    day_idx, slot_idx = _find_header(rows)
    columns = _column_map(rows[day_idx], rows[slot_idx])

    # Forward-fill column A into blocks: [(raw_group_name, [rows...])].
    blocks: list[tuple[str, list[list[str]]]] = []
    for row in rows[slot_idx + 1:]:
        head = _clean(row[0] if row else "")
        if head:
            blocks.append((head, [row]))
        elif blocks:
            blocks[-1][1].append(row)

    entries: list[ParsedEntry] = []
    found: set[str] = set()
    for raw_name, block_rows in blocks:
        target = match_group(raw_name, targets)
        if target is None:
            continue
        found.add(target)
        for col, (weekday, slot_no, start, end) in columns.items():
            lines: list[str] = []
            for row in block_rows:
                cell = row[col] if col < len(row) else ""
                lines.extend(part for part in (_clean(p) for p in cell.splitlines()) if part)
            if not lines:
                continue
            subject = lines[0]
            title, kind = split_subject(subject)
            room, teacher, shared = _classify(lines[1:])
            entries.append(
                ParsedEntry(
                    group_key=target, group_raw=raw_name, weekday=weekday, slot_no=slot_no,
                    start_time=start, end_time=end, subject=subject, kind=kind,
                    course_key=course_key(title), course_title=title,
                    room=room, teacher=teacher, shared_groups=shared, source=source,
                )
            )
    entries.sort(key=lambda e: (e.weekday, e.start_time, e.group_key))
    return entries, found
