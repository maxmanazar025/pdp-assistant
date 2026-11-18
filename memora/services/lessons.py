"""Class sessions, lesson logs and the automatic homework pipeline."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from memora import timeutil, ui
from memora.db.models import ClassSession, Course, LessonLog, TimetableEntry
from memora.services import actions
from memora.services import tasks as task_service
from memora.textfmt import esc, truncate
from memora.timetable import service as timetable


async def open_session(session: AsyncSession, entry: TimetableEntry, day: date) -> ClassSession:
    """Get or create the concrete occurrence of ``entry`` on ``day`` (idempotent)."""
    await session.execute(
        pg_insert(ClassSession).values(
            entry_id=entry.id, course_id=entry.course_id, class_date=day,
            starts_at=timeutil.combine(day, entry.start_time), ends_at=timeutil.combine(day, entry.end_time),
            subject=entry.subject, status="pending",
        ).on_conflict_do_nothing(constraint="uq_session_entry_date")
    )
    return (
        await session.execute(
            select(ClassSession).where(ClassSession.entry_id == entry.id, ClassSession.class_date == day)
        )
    ).scalar_one()


async def get_session(session: AsyncSession, session_id: int) -> ClassSession | None:
    return await session.get(ClassSession, session_id)


async def session_by_prompt(session: AsyncSession, message_id: int) -> ClassSession | None:
    result = await session.execute(select(ClassSession).where(ClassSession.prompt_message_id == message_id))
    return result.scalar_one_or_none()


async def pending_sessions(session: AsyncSession, days: int = 7) -> list[ClassSession]:
    """Finished classes of the last ``days`` days that still have no lesson log."""
    result = await session.execute(
        select(ClassSession).where(
            ClassSession.status == "pending",
            ClassSession.class_date >= timeutil.today() - timedelta(days=days),
            ClassSession.ends_at <= timeutil.now(),
        ).order_by(ClassSession.ends_at)
    )
    return list(result.scalars())


async def recent_logs(session: AsyncSession, course_id: int | None = None, limit: int = 5,
                      since: date | None = None) -> list[LessonLog]:
    stmt = select(LessonLog).order_by(LessonLog.class_date.desc(), LessonLog.id.desc()).limit(limit)
    if course_id is not None:
        stmt = stmt.where(LessonLog.course_id == course_id)
    if since is not None:
        stmt = stmt.where(LessonLog.class_date >= since)
    return list((await session.execute(stmt)).scalars())


async def last_log(session: AsyncSession, course_id: int, before: date | None = None) -> LessonLog | None:
    """Notes from the previous session of a course (used by '📝 Darsga tayyorgarlik')."""
    stmt = select(LessonLog).where(LessonLog.course_id == course_id)
    if before is not None:
        stmt = stmt.where(LessonLog.class_date < before)
    stmt = stmt.order_by(LessonLog.class_date.desc(), LessonLog.id.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def save_log(session: AsyncSession, *, course_id: int, class_date: date, topic: str,
                   homework: str | None, raw_text: str, input_kind: str,
                   class_session: ClassSession | None = None) -> LessonLog:
    log = LessonLog(
        course_id=course_id, session_id=class_session.id if class_session else None, class_date=class_date,
        topic=topic.strip(), homework=(homework or "").strip() or None, raw_text=raw_text, input_kind=input_kind,
    )
    session.add(log)
    if class_session is not None:
        class_session.status = "logged"
    await session.flush()
    await session.refresh(log, ["course"])
    return log


def homework_times(app, next_start: datetime | None, now: datetime) -> tuple[datetime, datetime]:  # noqa: ANN001
    """(due_at, remind_at) for homework: due when the next session starts, reminder the
    evening before — degraded gracefully when the next session is very close."""
    if next_start is None:
        due = timeutil.combine(now.date() + timedelta(days=7), app.settings.homework_reminder_at)
        return due, timeutil.combine(now.date() + timedelta(days=6), app.settings.homework_reminder_at)
    remind = timeutil.combine(next_start.date() - timedelta(days=1), app.settings.homework_reminder_at)
    if remind <= now + timedelta(minutes=30):
        remind = next_start - timedelta(hours=2)
    if remind <= now + timedelta(minutes=30):
        remind = min(now + timedelta(minutes=30), next_start - timedelta(minutes=20))
    if remind <= now:
        remind = now + timedelta(minutes=5)
    return next_start, remind


async def schedule_homework(session: AsyncSession, app, log: LessonLog) -> tuple[str, InlineKeyboardMarkup] | None:  # noqa: ANN001
    """Turn logged homework into a Google Task due before the next session of the course.

    With HOMEWORK_AUTO_CREATE=true the task is created immediately (with an undo button);
    otherwise a confirmation card is offered first.
    """
    if not log.homework:
        return None
    now = timeutil.now()
    course = await session.get(Course, log.course_id)
    upcoming = await timetable.next_occurrence(session, log.course_id, now)
    next_start = upcoming[0] if upcoming else None
    due_at, remind_at = homework_times(app, next_start, now)
    title = truncate(f"{course.title}: {log.homework}", 200)
    notes = f"Mavzu: {log.topic}\nUy vazifasi: {log.homework}\nDars sanasi: {log.class_date.isoformat()}"
    when = (
        f"Keyingi dars: <b>{timeutil.fmt_dt(next_start)}</b>" if next_start
        else "Keyingi dars jadvalda topilmadi — muddat bir hafta qilib qo'yildi"
    )
    if app.settings.homework_auto_create:
        task = await task_service.create_task(
            session, app, title, notes=notes, due_at=due_at, has_time=True, remind_at=remind_at,
            source="homework", course_id=log.course_id,
        )
        log.task_id = task.id
        text = (
            f"📚 <b>Uy vazifasi Google Tasks'ga qo'shildi</b>\n{esc(log.homework)}\n\n"
            f"{when}\n⏰ Eslatma: <b>{timeutil.fmt_dt(remind_at)}</b>"
        )
        return text, ui.task_created_kb(task.id)
    action = await actions.create_action(session, app, actions.TASK_CREATE, {
        "title": title, "notes": notes, "due_at": timeutil.iso(due_at), "remind_at": timeutil.iso(remind_at),
        "has_time": True, "course_id": log.course_id, "source": "homework",
    })
    text, keyboard = await actions.render_card(session, app, action)
    return f"{text}\n{when}", keyboard
