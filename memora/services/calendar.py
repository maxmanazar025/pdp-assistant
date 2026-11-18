"""Calendar service: Google Calendar access, local read cache and conflict detection."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from memora import timeutil
from memora.db.models import CalendarEvent
from memora.integrations.google import GoogleError, parse_event_times
from memora.timetable import service as timetable

log = logging.getLogger(__name__)

CACHE_BACK_DAYS = 1
CACHE_AHEAD_DAYS = 21


@dataclass(frozen=True, slots=True)
class Conflict:
    kind: str          # "event" | "class"
    title: str
    start: datetime
    end: datetime


def _row_values(event: dict) -> dict:
    start, end, all_day = parse_event_times(event)
    return {
        "google_event_id": event["id"], "summary": (event.get("summary") or "(nomsiz)")[:500],
        "location": (event.get("location") or None), "description": event.get("description"),
        "starts_at": start, "ends_at": end, "all_day": all_day, "html_link": event.get("htmlLink"),
        "synced_at": timeutil.now(),
    }


async def _upsert(session: AsyncSession, event: dict) -> None:
    values = _row_values(event)
    stmt = pg_insert(CalendarEvent).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[CalendarEvent.google_event_id],
        set_={k: v for k, v in values.items() if k != "google_event_id"},
    )
    await session.execute(stmt)


async def refresh_cache(session: AsyncSession, app) -> int:  # noqa: ANN001
    """Mirror the rolling window [-1d, +21d] of the user's calendar into Postgres."""
    if not app.google.enabled:
        return 0
    start = timeutil.day_bounds(timeutil.today() - timedelta(days=CACHE_BACK_DAYS))[0]
    end = start + timedelta(days=CACHE_BACK_DAYS + CACHE_AHEAD_DAYS)
    events = await app.google.list_events(start, end)
    ids = [e["id"] for e in events]
    await session.execute(
        delete(CalendarEvent).where(
            CalendarEvent.starts_at >= start, CalendarEvent.starts_at < end,
            CalendarEvent.google_event_id.not_in(ids) if ids else CalendarEvent.google_event_id.is_not(None),
        )
    )
    for event in events:
        await _upsert(session, event)
    return len(events)


async def cached_events(session: AsyncSession, start: datetime, end: datetime) -> list[CalendarEvent]:
    result = await session.execute(
        select(CalendarEvent)
        .where(CalendarEvent.starts_at < end, CalendarEvent.ends_at > start)
        .order_by(CalendarEvent.starts_at)
    )
    return list(result.scalars())


async def events_between(session: AsyncSession, app, start: datetime, end: datetime,  # noqa: ANN001
                         live: bool = False) -> list[CalendarEvent]:
    """Events overlapping [start, end). ``live=True`` re-reads Google first (falls back to cache)."""
    if live and app.google.enabled:
        try:
            for event in await app.google.list_events(start, end):
                await _upsert(session, event)
            await session.flush()
        except GoogleError as exc:
            log.warning("calendar live read failed, using cache", extra={"error": str(exc)})
    return await cached_events(session, start, end)


async def find_conflicts(session: AsyncSession, app, start: datetime, end: datetime,  # noqa: ANN001
                         ignore_event_id: str | None = None) -> list[Conflict]:
    """Everything that overlaps [start, end): calendar events *and* university classes."""
    conflicts: list[Conflict] = []
    for event in await events_between(session, app, start, end, live=True):
        if event.all_day or event.google_event_id == ignore_event_id:
            continue
        conflicts.append(Conflict("event", event.summary, event.starts_at, event.ends_at))
    day = timeutil.local(start).date()
    while day <= timeutil.local(end - timedelta(seconds=1)).date():
        for entry in await timetable.classes_on(session, day):
            c_start = timeutil.combine(day, entry.start_time)
            c_end = timeutil.combine(day, entry.end_time)
            if c_start < end and c_end > start:
                conflicts.append(Conflict("class", f"{entry.subject} ({entry.group_key})", c_start, c_end))
        day += timedelta(days=1)
    conflicts.sort(key=lambda c: c.start)
    return conflicts


async def create_event(session: AsyncSession, app, summary: str, start: datetime, end: datetime,  # noqa: ANN001
                       location: str | None = None, description: str | None = None) -> dict:
    event = await app.google.create_event(summary, start, end, location, description)
    await _upsert(session, event)
    return event


async def update_event(session: AsyncSession, app, event_id: str, **fields) -> dict:  # noqa: ANN001, ANN003
    event = await app.google.update_event(event_id, **fields)
    await _upsert(session, event)
    return event


async def delete_event(session: AsyncSession, app, event_id: str) -> None:  # noqa: ANN001
    await app.google.delete_event(event_id)
    await session.execute(delete(CalendarEvent).where(CalendarEvent.google_event_id == event_id))
