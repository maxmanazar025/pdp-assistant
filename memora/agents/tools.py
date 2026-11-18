"""Tool implementations exposed to the agents (function calling)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from memora import timeutil, ui
from memora.agents.base import Tool, ToolContext
from memora.db.models import CalendarEvent, Course
from memora.services import actions, lessons, quiz
from memora.services import calendar as cal
from memora.services import tasks as task_service
from memora.textfmt import esc
from memora.timetable import service as timetable

_DT = {"type": "string", "description": "Local datetime, format YYYY-MM-DDTHH:MM"}
_DATE = {"type": "string", "description": "Local date, format YYYY-MM-DD"}


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def _parse_dt(value: str) -> datetime:
    return timeutil.parse_iso_local(value)


def _parse_due(value: str) -> tuple[datetime, bool]:
    """Accepts a date (no time -> date-only task) or a datetime."""
    value = value.strip()
    if len(value) <= 10:
        return timeutil.combine(date.fromisoformat(value), time(0, 0)), False
    return _parse_dt(value), True


def _task_view(task) -> dict[str, Any]:  # noqa: ANN001
    return {
        "task_id": task.id, "title": task.title, "status": task.status,
        "due": (timeutil.iso(task.due_at) if task.has_time else timeutil.local(task.due_at).date().isoformat())
        if task.due_at else None,
        "reminder_at": timeutil.iso(task.remind_at) if task.remind_at else None,
        "source": task.source,
    }


async def find_course(ctx: ToolContext, name: str | None) -> Course | None:
    """Resolve a (possibly partial / misspelled) course name; defaults to the study-mode course."""
    courses = await timetable.list_courses(ctx.session)
    if name:
        needle = name.casefold().strip()
        for course in courses:
            if needle == course.key or needle == course.title.casefold():
                return course
        for course in courses:
            if needle in course.key or course.key in needle:
                return course
        tokens = [t for t in needle.replace("(", " ").replace(")", " ").split() if len(t) > 2]
        scored = [(sum(1 for t in tokens if t in c.key), c) for c in courses]
        best = max(scored, key=lambda pair: pair[0], default=(0, None))
        if best[0] > 0:
            return best[1]
    course_id = ctx.state_ctx.get("course_id")
    return await ctx.session.get(Course, course_id) if course_id else None


# ============================================================================ schedule / calendar
async def _get_schedule(ctx: ToolContext, args: dict) -> Any:
    start_day = date.fromisoformat(args["date_from"])
    end_day = date.fromisoformat(args.get("date_to") or args["date_from"])
    end_day = min(max(end_day, start_day), start_day + timedelta(days=13))
    window_start = timeutil.day_bounds(start_day)[0]
    window_end = timeutil.day_bounds(end_day)[1]
    await cal.events_between(ctx.session, ctx.app, window_start, window_end, live=True)
    days = []
    day = start_day
    while day <= end_day:
        start, end = timeutil.day_bounds(day)
        days.append({
            "date": day.isoformat(),
            "weekday": timeutil.WEEKDAYS_UZ[day.weekday()],
            "classes": [
                {"start": timeutil.fmt_time(c.start_time), "end": timeutil.fmt_time(c.end_time), "subject": c.subject,
                 "room": c.room, "teacher": c.teacher, "group": c.group_key}
                for c in await timetable.classes_on(ctx.session, day)
            ],
            "calendar_events": [
                {"event_id": e.google_event_id, "title": e.summary, "all_day": e.all_day,
                 "start": timeutil.iso(e.starts_at), "end": timeutil.iso(e.ends_at), "location": e.location}
                for e in await cal.cached_events(ctx.session, start, end)
            ],
            "tasks_due": [_task_view(t) for t in await task_service.tasks_due_between(ctx.session, start, end)],
        })
        day += timedelta(days=1)
    return {"days": days, "google_calendar_connected": ctx.app.google.enabled}


async def _find_free_slots(ctx: ToolContext, args: dict) -> Any:
    day = date.fromisoformat(args["date"])
    need = timedelta(minutes=int(args.get("duration_minutes") or 60))
    start, end = timeutil.combine(day, time(8, 0)), timeutil.combine(day, time(22, 0))
    start = max(start, timeutil.now()) if day == timeutil.today() else start
    busy = sorted((c.start, c.end) for c in await cal.find_conflicts(ctx.session, ctx.app, start, end))
    slots, cursor = [], start
    for b_start, b_end in busy:
        if b_start - cursor >= need:
            slots.append({"from": timeutil.fmt_time(cursor), "to": timeutil.fmt_time(b_start)})
        cursor = max(cursor, b_end)
    if end - cursor >= need:
        slots.append({"from": timeutil.fmt_time(cursor), "to": timeutil.fmt_time(end)})
    return {"date": day.isoformat(), "free_slots": slots}


def _require_google(ctx: ToolContext) -> None:
    if not ctx.app.google.enabled:
        raise RuntimeError("Google Calendar is not connected; tell the user to finish the Google setup")


async def _propose_event(ctx: ToolContext, args: dict) -> Any:
    _require_google(ctx)
    start = _parse_dt(args["start"])
    end = _parse_dt(args["end"]) if args.get("end") else start + timedelta(minutes=int(args.get("duration_minutes") or 60))
    if end <= start:
        end = start + timedelta(hours=1)
    action = await actions.create_action(ctx.session, ctx.app, actions.EVENT_CREATE, {
        "summary": args["title"].strip(), "start": timeutil.iso(start), "end": timeutil.iso(end),
        "location": args.get("location") or None, "description": args.get("description") or None,
    })
    ctx.cards.append(await actions.render_card(ctx.session, ctx.app, action))
    ctx.side_effects.append(f"tadbir taklif qilindi: {args['title']} {timeutil.iso(start)} (tasdiq kutilmoqda)")
    conflicts = await cal.find_conflicts(ctx.session, ctx.app, start, end)
    return {"status": "confirmation_card_shown_to_user", "not_created_yet": True,
            "conflicts": [{"title": c.title, "start": timeutil.iso(c.start), "end": timeutil.iso(c.end)} for c in conflicts]}


async def _find_events(ctx: ToolContext, args: dict) -> Any:
    start = timeutil.day_bounds(date.fromisoformat(args["date_from"]))[0]
    end = timeutil.day_bounds(date.fromisoformat(args.get("date_to") or args["date_from"]))[1]
    events = await cal.events_between(ctx.session, ctx.app, start, min(end, start + timedelta(days=31)), live=True)
    query = (args.get("query") or "").casefold()
    return {"events": [
        {"event_id": e.google_event_id, "title": e.summary, "start": timeutil.iso(e.starts_at),
         "end": timeutil.iso(e.ends_at), "location": e.location, "all_day": e.all_day}
        for e in events if not query or query in e.summary.casefold()
    ]}


async def _cached_event(ctx: ToolContext, event_id: str):  # noqa: ANN202
    event = await ctx.session.get(CalendarEvent, event_id)
    if event is None:
        raise ValueError("event_id not found; call find_events first")
    return event


async def _propose_event_update(ctx: ToolContext, args: dict) -> Any:
    _require_google(ctx)
    event = await _cached_event(ctx, args["event_id"])
    payload: dict[str, Any] = {
        "event_id": event.google_event_id, "old_summary": event.summary,
        "old_start": timeutil.iso(event.starts_at), "old_end": timeutil.iso(event.ends_at),
    }
    if args.get("new_title"):
        payload["summary"] = args["new_title"].strip()
    if args.get("new_start"):
        start = _parse_dt(args["new_start"])
        end = _parse_dt(args["new_end"]) if args.get("new_end") else start + (event.ends_at - event.starts_at)
        payload["start"], payload["end"] = timeutil.iso(start), timeutil.iso(end)
    elif args.get("new_end"):
        payload["start"], payload["end"] = timeutil.iso(event.starts_at), timeutil.iso(_parse_dt(args["new_end"]))
    if args.get("new_location"):
        payload["location"] = args["new_location"]
    action = await actions.create_action(ctx.session, ctx.app, actions.EVENT_UPDATE, payload)
    ctx.cards.append(await actions.render_card(ctx.session, ctx.app, action))
    ctx.side_effects.append(f"tadbirni o'zgartirish taklif qilindi: {event.summary} (tasdiq kutilmoqda)")
    return {"status": "confirmation_card_shown_to_user", "not_updated_yet": True}


async def _propose_event_delete(ctx: ToolContext, args: dict) -> Any:
    _require_google(ctx)
    event = await _cached_event(ctx, args["event_id"])
    action = await actions.create_action(ctx.session, ctx.app, actions.EVENT_DELETE, {
        "event_id": event.google_event_id, "summary": event.summary,
        "start": timeutil.iso(event.starts_at), "end": timeutil.iso(event.ends_at),
    })
    ctx.cards.append(await actions.render_card(ctx.session, ctx.app, action))
    ctx.side_effects.append(f"tadbirni o'chirish taklif qilindi: {event.summary} (tasdiq kutilmoqda)")
    return {"status": "confirmation_card_shown_to_user", "not_deleted_yet": True}


# ============================================================================ tasks
async def _list_tasks(ctx: ToolContext, args: dict) -> Any:
    return {"open_tasks": [_task_view(t) for t in await task_service.open_tasks(ctx.session, limit=40)]}


async def _create_task(ctx: ToolContext, args: dict) -> Any:
    due_at, has_time = _parse_due(args["due"]) if args.get("due") else (None, False)
    task = await task_service.create_task(
        ctx.session, ctx.app, args["title"], notes=args.get("notes") or None, due_at=due_at, has_time=has_time,
    )
    lines = [f"✅ <b>Vazifa qo'shildi:</b> {esc(task.title)}"]
    if task.remind_at:
        lines.append(f"⏰ Eslatma: <b>{timeutil.fmt_dt(task.remind_at)}</b>")
    if task.needs_push and ctx.app.google.enabled:
        lines.append("<i>Google Tasks hozir javob bermadi — keyinroq avtomatik sinxronlanadi.</i>")
    ctx.cards.append(("\n".join(lines), ui.task_created_kb(task.id)))
    ctx.side_effects.append(f"vazifa yaratildi #{task.id}: {task.title}")
    return {"status": "created", "receipt_shown_to_user": True, **_task_view(task)}


async def _load_task(ctx: ToolContext, args: dict):  # noqa: ANN202
    task = await task_service.get_task(ctx.session, int(args["task_id"]))
    if task is None or task.status == "deleted":
        raise ValueError("task_id not found; call list_tasks first")
    return task


async def _complete_task(ctx: ToolContext, args: dict) -> Any:
    task = await task_service.complete_task(ctx.session, ctx.app, await _load_task(ctx, args))
    ctx.cards.append((f"✅ <b>Bajarildi:</b> {esc(task.title)}", ui.task_done_kb(task.id)))
    ctx.side_effects.append(f"vazifa bajarildi #{task.id}")
    return {"status": "completed", "receipt_shown_to_user": True, "task_id": task.id}


async def _reschedule_task(ctx: ToolContext, args: dict) -> Any:
    task = await _load_task(ctx, args)
    due_at, has_time = _parse_due(args["new_due"])
    await task_service.reschedule_task(ctx.session, ctx.app, task, due_at, has_time)
    when = timeutil.fmt_dt(task.remind_at) if task.remind_at else "—"
    ctx.cards.append((f"🔁 <b>Ko'chirildi:</b> {esc(task.title)}\n⏰ Yangi eslatma: <b>{when}</b>", None))
    ctx.side_effects.append(f"vazifa ko'chirildi #{task.id} -> {args['new_due']}")
    return {"status": "rescheduled", "receipt_shown_to_user": True, **_task_view(task)}


async def _propose_task_delete(ctx: ToolContext, args: dict) -> Any:
    task = await _load_task(ctx, args)
    action = await actions.create_action(ctx.session, ctx.app, actions.TASK_DELETE,
                                         {"task_id": task.id, "title": task.title})
    ctx.cards.append(await actions.render_card(ctx.session, ctx.app, action))
    ctx.side_effects.append(f"vazifani o'chirish taklif qilindi #{task.id} (tasdiq kutilmoqda)")
    return {"status": "confirmation_card_shown_to_user", "not_deleted_yet": True}


# ============================================================================ study
async def _get_lesson_logs(ctx: ToolContext, args: dict) -> Any:
    course = await find_course(ctx, args.get("course")) if args.get("course") else None
    since = timeutil.today() - timedelta(days=int(args.get("days") or 30))
    logs = await lessons.recent_logs(ctx.session, course.id if course else None,
                                     limit=min(int(args.get("limit") or 8), 20), since=since)
    return {"course_filter": course.title if course else None, "lessons": [
        {"date": entry.class_date.isoformat(), "course": entry.course.title, "topic": entry.topic,
         "homework": entry.homework} for entry in logs
    ]}


async def _list_courses(ctx: ToolContext, args: dict) -> Any:
    return {"courses": [{"title": c.title, "group": c.group_key} for c in await timetable.list_courses(ctx.session)]}


async def _get_weak_topics(ctx: ToolContext, args: dict) -> Any:
    weak = await quiz.weak_topics(ctx.session, limit=8)
    return {"weak_topics": [
        {"course": w.course, "topic": w.topic, "attempts": w.attempts, "accuracy_percent": round(w.accuracy * 100)}
        for w in weak
    ]}


async def _start_quiz(ctx: ToolContext, args: dict) -> Any:
    course = await find_course(ctx, args.get("course"))
    logs = await lessons.recent_logs(ctx.session, course.id if course else None, limit=5) if course else []
    count = max(1, min(int(args.get("count") or 5), 10))
    items = await quiz.generate_items(ctx.session, ctx.app, course=course, topic=args.get("topic"), logs=logs, count=count)
    if not items:
        raise RuntimeError("could not generate valid questions; try again with a clearer topic")
    await quiz.send_batch(ctx.session, ctx.app, items)
    ctx.side_effects.append(f"{len(items)} ta test savoli yuborildi")
    return {"status": "quiz_polls_sent_to_user", "questions": len(items),
            "note": "Do not repeat the questions; just tell the user in one short sentence that the quiz was sent."}


# ============================================================================ registry
GET_SCHEDULE = Tool(
    "get_schedule",
    "University classes, Google Calendar events and tasks due for a date or date range (max 14 days).",
    _obj({"date_from": _DATE, "date_to": _DATE}, ["date_from"]), _get_schedule, read_only=True)
FIND_FREE_SLOTS = Tool(
    "find_free_slots", "Free time windows on a date between 08:00 and 22:00, avoiding classes and calendar events.",
    _obj({"date": _DATE, "duration_minutes": {"type": "integer"}}, ["date"]), _find_free_slots, read_only=True)
FIND_EVENTS = Tool(
    "find_events", "Search Google Calendar events (returns event_id needed for update/delete).",
    _obj({"date_from": _DATE, "date_to": _DATE, "query": {"type": "string"}}, ["date_from"]), _find_events,
    read_only=True)
PROPOSE_EVENT = Tool(
    "propose_event",
    "Propose a NEW Google Calendar event. Shows the user a confirmation card with buttons; the event is created "
    "only after the user confirms. Conflicts are checked automatically.",
    _obj({"title": {"type": "string"}, "start": _DT, "end": _DT, "duration_minutes": {"type": "integer"},
          "location": {"type": "string"}, "description": {"type": "string"}}, ["title", "start"]), _propose_event)
PROPOSE_EVENT_UPDATE = Tool(
    "propose_event_update", "Propose changing an existing calendar event (needs event_id). Requires user confirmation.",
    _obj({"event_id": {"type": "string"}, "new_title": {"type": "string"}, "new_start": _DT, "new_end": _DT,
          "new_location": {"type": "string"}}, ["event_id"]), _propose_event_update)
PROPOSE_EVENT_DELETE = Tool(
    "propose_event_delete", "Propose deleting a calendar event (needs event_id). Requires user confirmation.",
    _obj({"event_id": {"type": "string"}}, ["event_id"]), _propose_event_delete)

LIST_TASKS = Tool("list_tasks", "All open tasks/reminders with their task_id, due and reminder times.",
                  _obj({}), _list_tasks, read_only=True)
CREATE_TASK = Tool(
    "create_task",
    "Create a task / reminder in Google Tasks immediately. 'due' is a datetime (YYYY-MM-DDTHH:MM) for a timed "
    "reminder or a date (YYYY-MM-DD) for an all-day task; omit it for a task without a deadline. "
    "A receipt with buttons is shown to the user automatically.",
    _obj({"title": {"type": "string", "description": "Short task title in Uzbek"}, "due": {"type": "string"},
          "notes": {"type": "string"}}, ["title"]), _create_task)
COMPLETE_TASK = Tool("complete_task", "Mark a task as done (needs task_id from list_tasks).",
                     _obj({"task_id": {"type": "integer"}}, ["task_id"]), _complete_task)
RESCHEDULE_TASK = Tool(
    "reschedule_task", "Move a task to a new date or datetime (needs task_id).",
    _obj({"task_id": {"type": "integer"}, "new_due": {"type": "string", "description": "YYYY-MM-DDTHH:MM or YYYY-MM-DD"}},
         ["task_id", "new_due"]), _reschedule_task)
PROPOSE_TASK_DELETE = Tool(
    "propose_task_delete", "Propose deleting a task (needs task_id). Requires user confirmation.",
    _obj({"task_id": {"type": "integer"}}, ["task_id"]), _propose_task_delete)

GET_LESSON_LOGS = Tool(
    "get_lesson_logs",
    "What was actually covered in the student's recent classes: date, course, topic and homework.",
    _obj({"course": {"type": "string", "description": "Course name filter (optional)"},
          "days": {"type": "integer", "description": "Look-back window, default 30"},
          "limit": {"type": "integer"}}), _get_lesson_logs, read_only=True)
LIST_COURSES = Tool("list_courses", "Courses in the student's current timetable.", _obj({}), _list_courses,
                    read_only=True)
GET_WEAK_TOPICS = Tool("get_weak_topics", "Topics where the student's quiz accuracy is low.", _obj({}),
                       _get_weak_topics, read_only=True)
START_QUIZ = Tool(
    "start_quiz",
    "Generate an active-recall quiz and send it to the user as native Telegram quiz polls. "
    "Use when the user asks to be tested / quizzed.",
    _obj({"course": {"type": "string"}, "topic": {"type": "string"},
          "count": {"type": "integer", "description": "1-10, default 5"}}), _start_quiz)
