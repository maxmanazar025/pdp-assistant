"""Read-side queries over the stored timetable."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from memora import timeutil
from memora.db.models import Course, TimetableEntry


def group_label(entry: TimetableEntry) -> str:
    return entry.group_key


async def classes_on(session: AsyncSession, day: date) -> list[TimetableEntry]:
    """Active classes of both cohorts for a calendar day, in chronological order."""
    result = await session.execute(
        select(TimetableEntry)
        .where(TimetableEntry.is_active.is_(True), TimetableEntry.weekday == day.weekday())
        .order_by(TimetableEntry.start_time, TimetableEntry.group_key)
    )
    return list(result.scalars())


async def all_active(session: AsyncSession) -> list[TimetableEntry]:
    result = await session.execute(
        select(TimetableEntry)
        .where(TimetableEntry.is_active.is_(True))
        .order_by(TimetableEntry.weekday, TimetableEntry.start_time)
    )
    return list(result.scalars())


async def list_courses(session: AsyncSession) -> list[Course]:
    """Courses that currently have at least one active slot."""
    result = await session.execute(
        select(Course)
        .where(Course.id.in_(select(TimetableEntry.course_id).where(TimetableEntry.is_active.is_(True))))
        .order_by(Course.group_key, Course.title)
    )
    return list(result.scalars())


async def next_occurrence(
    session: AsyncSession, course_id: int, after: datetime, horizon_days: int = 21
) -> tuple[datetime, TimetableEntry] | None:
    """First session of a course that starts strictly after ``after``."""
    result = await session.execute(
        select(TimetableEntry).where(TimetableEntry.is_active.is_(True), TimetableEntry.course_id == course_id)
    )
    entries = list(result.scalars())
    if not entries:
        return None
    after = timeutil.local(after)
    best: tuple[datetime, TimetableEntry] | None = None
    for offset in range(horizon_days + 1):
        day = after.date() + timedelta(days=offset)
        for entry in entries:
            if entry.weekday != day.weekday():
                continue
            start = timeutil.combine(day, entry.start_time)
            if start > after and (best is None or start < best[0]):
                best = (start, entry)
        if best is not None:
            return best
    return None


def continues_same_course(entry: TimetableEntry, day_entries: list[TimetableEntry], gap_minutes: int = 20) -> bool:
    """True if another slot of the same course follows this one back-to-back.

    Double periods (e.g. two consecutive Python practices) get one reflection prompt,
    after the last slot, instead of interrupting the student between them.
    """
    end = datetime.combine(date.min, entry.end_time)
    for other in day_entries:
        if other.id == entry.id or other.course_id != entry.course_id:
            continue
        gap = (datetime.combine(date.min, other.start_time) - end).total_seconds() / 60
        if 0 <= gap <= gap_minutes:
            return True
    return False
