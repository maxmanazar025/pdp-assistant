"""Task & reminder service.

Postgres is the source of truth for *when to remind* (Google Tasks only stores a due
date, never a time), Google Tasks is the user-facing list. Writes are local-first with an
outbox flag (``needs_push``): if Google is unreachable the bot still answers instantly
and the worker pushes the change later. A periodic pull brings in edits made in the
Google Tasks app (completions, deletions, new tasks).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from memora import timeutil
from memora.db import kv_get, kv_set
from memora.db.models import Task
from memora.integrations.google import GoogleError

log = logging.getLogger(__name__)

_MARKER_RE = re.compile(r"\n*⏰ Eslatma: \S+\s*$")


def _default_remind(app, due_at: datetime | None, has_time: bool) -> datetime | None:  # noqa: ANN001
    if due_at is None:
        return None
    if has_time:
        return due_at
    return timeutil.combine(timeutil.local(due_at).date(), app.settings.date_only_reminder_at)


def _google_notes(task: Task) -> str | None:
    """Notes as shown in Google Tasks: user notes + the exact ISO-8601 reminder timestamp."""
    notes = (task.notes or "").strip()
    if task.remind_at is not None:
        notes = f"{notes}\n\n⏰ Eslatma: {timeutil.iso(task.remind_at)}".strip()
    return notes or None


def _google_body(task: Task) -> dict:
    body: dict = {"title": task.title, "notes": _google_notes(task) or ""}
    body["due"] = (
        f"{timeutil.local(task.due_at).date().isoformat()}T00:00:00.000Z" if task.due_at is not None else None
    )
    body["status"] = "completed" if task.status == "completed" else "needsAction"
    if task.status != "completed":
        body["completed"] = None
    return body


async def _push(app, task: Task) -> None:  # noqa: ANN001
    """Push one task to Google. Raises GoogleError on failure (caller keeps needs_push)."""
    google = app.google
    if task.status == "deleted":
        if task.google_task_id:
            await google.delete_task(task.google_task_id)
    elif task.google_task_id is None:
        created = await google.create_task(
            task.title, _google_notes(task), timeutil.local(task.due_at).date() if task.due_at else None
        )
        task.google_task_id = created["id"]
        if task.status == "completed":
            await google.patch_task(created["id"], {"status": "completed"})
    else:
        await google.patch_task(task.google_task_id, _google_body(task))
    task.needs_push = False
    task.google_updated = timeutil.now()


async def _try_push(app, task: Task) -> None:  # noqa: ANN001
    task.needs_push = True
    if not app.google.enabled:
        return
    try:
        await _push(app, task)
    except GoogleError as exc:
        log.warning("google task push deferred", extra={"task_id": task.id, "error": str(exc)})


# ------------------------------------------------------------------------------ commands
async def create_task(session: AsyncSession, app, title: str, *, notes: str | None = None,  # noqa: ANN001
                      due_at: datetime | None = None, has_time: bool = False,
                      remind_at: datetime | None = None, source: str = "bot",
                      course_id: int | None = None) -> Task:
    task = Task(
        title=title.strip()[:500], notes=(notes or None), due_at=due_at, has_time=has_time,
        remind_at=remind_at or _default_remind(app, due_at, has_time), status="pending",
        source=source, course_id=course_id,
    )
    session.add(task)
    await session.flush()
    await _try_push(app, task)
    return task


async def complete_task(session: AsyncSession, app, task: Task) -> Task:  # noqa: ANN001
    if task.status != "completed":
        task.status = "completed"
        task.completed_at = timeutil.now()
        await _try_push(app, task)
    return task


async def reopen_task(session: AsyncSession, app, task: Task) -> Task:  # noqa: ANN001
    if task.status == "completed":
        task.status = "pending"
        task.completed_at = None
        await _try_push(app, task)
    return task


async def reschedule_task(session: AsyncSession, app, task: Task, due_at: datetime, has_time: bool) -> Task:  # noqa: ANN001
    task.due_at = due_at
    task.has_time = has_time
    task.remind_at = _default_remind(app, due_at, has_time)
    await _try_push(app, task)
    return task


async def snooze_task(session: AsyncSession, app, task: Task, minutes: int) -> Task:  # noqa: ANN001
    """Move only the reminder (counted from now); the due date stays untouched."""
    task.remind_at = timeutil.now() + timedelta(minutes=minutes)
    await _try_push(app, task)
    return task


async def delete_task(session: AsyncSession, app, task: Task) -> None:  # noqa: ANN001
    task.status = "deleted"
    await _try_push(app, task)


# ------------------------------------------------------------------------------ queries
async def get_task(session: AsyncSession, task_id: int) -> Task | None:
    return await session.get(Task, task_id)


async def open_tasks(session: AsyncSession, limit: int = 30) -> list[Task]:
    result = await session.execute(
        select(Task).where(Task.status == "pending")
        .order_by(Task.due_at.asc().nulls_last(), Task.id.asc()).limit(limit)
    )
    return list(result.scalars())


async def tasks_due_between(session: AsyncSession, start: datetime, end: datetime) -> list[Task]:
    result = await session.execute(
        select(Task).where(Task.status == "pending", Task.due_at >= start, Task.due_at < end).order_by(Task.due_at)
    )
    return list(result.scalars())


async def overdue_tasks(session: AsyncSession, before: datetime) -> list[Task]:
    result = await session.execute(
        select(Task).where(Task.status == "pending", Task.due_at < before).order_by(Task.due_at)
    )
    return list(result.scalars())


async def due_reminders(session: AsyncSession, now: datetime) -> list[Task]:
    """Pending tasks whose reminder time has arrived and has not fired for that time yet."""
    result = await session.execute(
        select(Task).where(
            Task.status == "pending",
            Task.remind_at.is_not(None),
            Task.remind_at <= now,
            or_(Task.reminded_at.is_(None), Task.reminded_at < Task.remind_at),
        ).order_by(Task.remind_at)
    )
    return list(result.scalars())


# ------------------------------------------------------------------------------ Google sync
async def push_pending(session: AsyncSession, app) -> tuple[int, GoogleError | None]:  # noqa: ANN001
    """Flush the outbox. Each task is independent: one failure never rolls back another
    task's successful push (which would otherwise be created twice in Google)."""
    if not app.google.enabled:
        return 0, None
    result = await session.execute(select(Task).where(Task.needs_push.is_(True)).order_by(Task.id).limit(50))
    pushed = 0
    failure: GoogleError | None = None
    for task in result.scalars():
        try:
            await _push(app, task)
            pushed += 1
        except GoogleError as exc:
            if exc.status == 404 and task.google_task_id:  # removed in Google meanwhile
                task.google_task_id = None
                if task.status == "deleted":
                    task.needs_push = False
                continue
            failure = exc
            if exc.status in (400, 401, None):  # auth/network trouble: the rest would fail too
                break
    return pushed, failure


async def pull_google(session: AsyncSession, app) -> dict[str, int]:  # noqa: ANN001
    """Import changes made in the Google Tasks app."""
    stats = {"imported": 0, "completed": 0, "deleted": 0, "updated": 0}
    if not app.google.enabled:
        return stats
    cursor = await kv_get(session, "gtasks:cursor")
    first_run = cursor is None
    started = timeutil.now()
    items = await app.google.list_tasks(timeutil.parse_iso_local(cursor) if cursor else None)
    known = {
        t.google_task_id: t
        for t in (await session.execute(select(Task).where(Task.google_task_id.is_not(None)))).scalars()
    }
    for item in items:
        gid = item["id"]
        title = (item.get("title") or "").strip()
        deleted = bool(item.get("deleted"))
        completed = item.get("status") == "completed"
        due_day = date.fromisoformat(item["due"][:10]) if item.get("due") else None
        notes = _MARKER_RE.sub("", item.get("notes") or "").strip() or None
        task = known.get(gid)
        if task is None:
            if deleted or not title or (completed and first_run):
                continue
            if completed:
                continue  # completed elsewhere and never tracked here: nothing to remind about
            due_at = timeutil.combine(due_day, datetime.min.time()) if due_day else None
            task = Task(
                google_task_id=gid, title=title[:500], notes=notes, due_at=due_at, has_time=False,
                remind_at=_default_remind(app, due_at, False), status="pending", source="google",
                google_updated=started,
            )
            if task.remind_at is not None and task.remind_at <= started:
                task.reminded_at = started  # do not fire a stale reminder for an old imported task
            session.add(task)
            stats["imported"] += 1
            continue
        if task.needs_push:
            continue  # local change not pushed yet wins
        if deleted:
            if task.status != "deleted":
                task.status = "deleted"
                stats["deleted"] += 1
            continue
        if completed and task.status == "pending":
            task.status, task.completed_at = "completed", started
            stats["completed"] += 1
        elif not completed and task.status == "completed":
            task.status, task.completed_at = "pending", None
            stats["updated"] += 1
        if title and title != task.title:
            task.title = title[:500]
            stats["updated"] += 1
        if notes != (task.notes or None):
            task.notes = notes
        local_day = timeutil.local(task.due_at).date() if task.due_at else None
        if due_day != local_day:
            task.due_at = timeutil.combine(due_day, datetime.min.time()) if due_day else None
            task.has_time = False
            task.remind_at = _default_remind(app, task.due_at, False)
            stats["updated"] += 1
    await session.flush()
    await kv_set(session, "gtasks:cursor", timeutil.iso(started - timedelta(minutes=2)))
    return stats
