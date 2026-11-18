"""Deterministic Uzbek (Latin) renderers for every menu screen and proactive message.

Nothing in this module calls an LLM or the network: all screens are built from Postgres,
which is what keeps the reply-keyboard fast path well under 300 ms.
"""
from __future__ import annotations

from datetime import date, timedelta

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from memora import timeutil, ui
from memora.db.models import ClassSession, Course, LessonLog, StudySession, Task, TimetableEntry
from memora.services import calendar as cal
from memora.services import lessons, quiz
from memora.services import tasks as task_service
from memora.textfmt import esc, truncate
from memora.timetable import service as timetable


def _class_line(entry: TimetableEntry) -> str:
    meta = []
    if entry.room:
        meta.append(f"📍 {esc(entry.room)}")
    if entry.teacher:
        meta.append(f"👤 {esc(entry.teacher)}")
    meta.append(f"👥 {esc(entry.group_key)}")
    return (
        f"<b>{timeutil.fmt_time(entry.start_time)}–{timeutil.fmt_time(entry.end_time)}</b> · {esc(entry.subject)}\n"
        f"      {' · '.join(meta)}"
    )


def _task_line(task: Task, index: int | None = None, ref: date | None = None) -> str:
    prefix = f"{index}. " if index is not None else "• "
    line = f"{prefix}{esc(truncate(task.title, 120))}"
    if task.due_at is not None:
        due = timeutil.local(task.due_at)
        when = timeutil.fmt_dt(due, ref) if task.has_time else timeutil.fmt_day_label(due.date(), ref)
        overdue = due < timeutil.now() if task.has_time else due.date() < (ref or timeutil.today())
        line += f" — {'❗️' if overdue else '⏰'} <i>{when}</i>"
    return line


# ------------------------------------------------------------------------------ schedule
async def render_day(session: AsyncSession, app, day: date) -> str:  # noqa: ANN001
    today = timeutil.today()
    title = {0: "Bugun", 1: "Ertaga"}.get((day - today).days)
    header = f"{title} — {timeutil.fmt_date(day)}" if title else timeutil.fmt_date(day).capitalize()
    lines = [f"📅 <b>{header}</b>", ""]

    classes = await timetable.classes_on(session, day)
    if classes:
        lines.append(f"🎓 <b>Darslar ({len(classes)} ta)</b>")
        lines += [_class_line(c) for c in classes]
    else:
        lines.append("🎓 Dars yo'q — bo'sh kun 🎉")

    start, end = timeutil.day_bounds(day)
    events = await cal.cached_events(session, start, end)
    if events:
        lines += ["", f"🗓 <b>Tadbirlar ({len(events)} ta)</b>"]
        for event in events:
            when = "kun bo'yi" if event.all_day else (
                f"{timeutil.fmt_time(event.starts_at)}–{timeutil.fmt_time(event.ends_at)}")
            place = f" · 📍 {esc(event.location)}" if event.location else ""
            lines.append(f"<b>{when}</b> · {esc(event.summary)}{place}")

    due = await task_service.tasks_due_between(session, start, end)
    overdue = await task_service.overdue_tasks(session, start) if day == today else []
    if due or overdue:
        lines += ["", "📋 <b>Vazifalar</b>"]
        lines += [_task_line(t, ref=today) for t in overdue[:5]]
        lines += [_task_line(t, ref=today) for t in due[:10]]
    if not app.google.enabled:
        lines += ["", "<i>Google Calendar ulanmagan — faqat dars jadvali ko'rsatilmoqda.</i>"]
    return "\n".join(lines)


async def render_tasks(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup | None]:
    tasks = await task_service.open_tasks(session, limit=15)
    if not tasks:
        return ("📋 <b>Vazifalarim</b>\n\nHozircha ochiq vazifa yo'q. ✨\n"
                "<i>Yangi vazifa uchun shunchaki yozing: «ertaga 18:00 da laboratoriya ishini topshirish»</i>"), None
    today = timeutil.today()
    lines = [f"📋 <b>Vazifalarim ({len(tasks)} ta)</b>", ""]
    lines += [_task_line(t, i, today) for i, t in enumerate(tasks, 1)]
    lines += ["", "<i>Bajarilganini belgilash uchun raqamini bosing.</i>"]
    return "\n".join(lines), ui.task_list_kb([t.id for t in tasks[:10]])


async def render_briefing(session: AsyncSession, app) -> tuple[str, InlineKeyboardMarkup]:  # noqa: ANN001
    today = timeutil.today()
    body = await render_day(session, app, today)
    body = body.replace("📅 <b>Bugun — ", f"☀️ <b>Xayrli tong, {esc(app.settings.owner_name)}!</b>\n📅 <b>Bugun — ", 1)
    pending = await lessons.pending_sessions(session)
    due_cards = await quiz.due_count(session)
    extra = []
    if pending:
        extra.append(f"📝 Yozilmagan darslar: <b>{len(pending)} ta</b>")
    if due_cards:
        extra.append(f"🧠 Takrorlash uchun savollar: <b>{due_cards} ta</b>")
    if extra:
        body += "\n\n" + "\n".join(extra)
    return body, ui.briefing_kb(bool(pending), bool(due_cards))


# ------------------------------------------------------------------------------ classes
def render_class_alert(entry: TimetableEntry, minutes_left: int) -> str:
    lines = [
        f"🔔 <b>{minutes_left} daqiqadan keyin dars</b>",
        "",
        f"📘 <b>{esc(entry.subject)}</b>",
        f"🕒 {timeutil.fmt_time(entry.start_time)}–{timeutil.fmt_time(entry.end_time)}",
        f"📍 {esc(entry.room or "xona ko'rsatilmagan")}",
    ]
    if entry.teacher:
        lines.append(f"👤 {esc(entry.teacher)}")
    lines.append(f"👥 {esc(entry.group_key)}")
    return "\n".join(lines)


async def render_prep(session: AsyncSession, entry: TimetableEntry) -> tuple[str, InlineKeyboardMarkup | None]:
    """'📝 Darsga tayyorgarlik': notes + homework from the previous session of this subject."""
    course = entry.course
    previous = await lessons.last_log(session, entry.course_id)
    lines = [f"📝 <b>Tayyorgarlik: {esc(course.title)}</b>", ""]
    if previous is None:
        lines.append("Bu fandan hali dars qaydi yo'q. Dars tugagach mavzu va uy vazifasini yozib qo'yaman.")
    else:
        lines += [
            f"<b>Oldingi dars</b> ({timeutil.fmt_date(previous.class_date)}):",
            f"📖 Mavzu: {esc(previous.topic)}",
            f"📚 Uy vazifasi: {esc(previous.homework) if previous.homework else 'berilmagan'}",
        ]
    open_hw = (
        await session.execute(
            select(Task).where(Task.status == "pending", Task.course_id == entry.course_id).order_by(Task.due_at)
        )
    ).scalars().all()
    if open_hw:
        lines += ["", "⚠️ <b>Bajarilmagan vazifalar:</b>"]
        lines += [_task_line(t) for t in open_hw[:5]]
    elif previous is not None and previous.homework:
        lines += ["", "✅ Bu fan bo'yicha ochiq vazifa qolmagan."]
    return "\n".join(lines), ui.prep_kb(entry.course_id, previous is not None)


def render_lesson_prompt(class_session: ClassSession) -> str:
    return (
        f"✅ <b>{esc(class_session.subject)}</b> darsi tugadi "
        f"({timeutil.fmt_time(class_session.starts_at)}–{timeutil.fmt_time(class_session.ends_at)}).\n\n"
        "Bugun qaysi <b>mavzu</b> o'tildi va <b>uy vazifasi</b> berildimi?\n"
        "<i>Matn yozing yoki ovozli xabar yuboring 🎙</i>"
    )


def render_reminder(task: Task) -> str:
    lines = ["⏰ <b>Eslatma</b>", "", f"<b>{esc(task.title)}</b>"]
    if task.due_at is not None:
        due = timeutil.local(task.due_at)
        label = timeutil.fmt_dt(due) if task.has_time else timeutil.fmt_day_label(due.date())
        lines.append(f"📅 Muddat: {label}")
    if task.notes:
        lines.append(f"\n<i>{esc(truncate(task.notes, 300))}</i>")
    return "\n".join(lines)


async def render_pending_lessons(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup | None]:
    pending = await lessons.pending_sessions(session)
    if not pending:
        return "📝 Yozilmagan dars yo'q — hammasi qayd etilgan. 👏", None
    items = [
        (s.id, truncate(f"{timeutil.fmt_day_label(s.class_date)} {timeutil.fmt_time(s.starts_at)} · {s.subject}", 55))
        for s in pending[-8:]
    ]
    return ("📝 <b>Yozilmagan darslar</b>\nQaysi dars haqida yozmoqchisiz?", ui.pending_lessons_kb(items))


# ------------------------------------------------------------------------------ weekly digest
async def render_weekly(session: AsyncSession, ref: date | None = None) -> str:
    monday, sunday = timeutil.week_bounds(ref or timeutil.today())
    start = timeutil.day_bounds(monday)[0]
    end = timeutil.day_bounds(sunday)[1]
    now = timeutil.now()

    sessions = (
        await session.execute(
            select(ClassSession).where(ClassSession.class_date >= monday, ClassSession.class_date <= sunday)
        )
    ).scalars().all()
    held = [s for s in sessions if s.status != "cancelled"]
    logged = [s for s in held if s.status == "logged"]
    logs = (
        await session.execute(
            select(LessonLog).where(LessonLog.class_date >= monday, LessonLog.class_date <= sunday)
            .order_by(LessonLog.class_date)
        )
    ).scalars().all()

    completed = (
        await session.execute(
            select(func.count(Task.id)).where(Task.status == "completed", Task.completed_at >= start,
                                              Task.completed_at < end)
        )
    ).scalar_one()
    pending = (await session.execute(select(func.count(Task.id)).where(Task.status == "pending"))).scalar_one()
    overdue = len(await task_service.overdue_tasks(session, now))

    studies = (
        await session.execute(
            select(StudySession).where(StudySession.started_at >= start, StudySession.started_at < end)
        )
    ).scalars().all()
    study_minutes = sum(
        int(((s.ended_at or min(now, s.started_at + timedelta(hours=2))) - s.started_at).total_seconds() // 60)
        for s in studies
    )
    attempts, correct = await quiz.stats_between(session, start, end)
    weak = await quiz.weak_topics(session, limit=4)

    lines = [
        "📊 <b>Haftalik hisobot</b>",
        f"<i>{timeutil.fmt_date(monday, False)} – {timeutil.fmt_date(sunday, False)}</i>",
        "",
        "🎓 <b>Darslar</b>",
        f"• Qayd etilgan: <b>{len(logged)}</b> / {len(held)}" if held else f"• Qayd etilgan darslar: <b>{len(logs)}</b>",
    ]
    by_course: dict[str, list[str]] = {}
    for entry in logs:
        by_course.setdefault(entry.course.title, []).append(entry.topic)
    for title, topics in by_course.items():
        lines.append(f"   📘 {esc(title)}: {esc(truncate('; '.join(topics), 160))}")

    total = completed + pending
    rate = f" ({round(100 * completed / total)}%)" if total else ""
    lines += [
        "",
        "📋 <b>Vazifalar</b>",
        f"• Bajarildi: <b>{completed}</b>{rate}",
        f"• Kutilmoqda: <b>{pending}</b>" + (f" (shundan ❗️ {overdue} tasi muddati o'tgan)" if overdue else ""),
        "",
        "📖 <b>O'qish</b>",
        f"• Mashg'ulotlar: <b>{len(studies)}</b> ta" + (f", jami {timeutil.fmt_duration(study_minutes)}" if studies else ""),
    ]
    if attempts:
        lines.append(f"• Test savollari: <b>{correct}</b> / {attempts} to'g'ri ({round(100 * correct / attempts)}%)")
    else:
        lines.append("• Test ishlanmadi")
    if weak:
        lines += ["", "🎯 <b>E'tibor talab mavzular</b>"]
        lines += [f"• {esc(w.topic)} — {round(w.accuracy * 100)}% ({esc(w.course or 'umumiy')})" for w in weak]

    tips = []
    if held and len(logged) < len(held):
        tips.append(f"{len(held) - len(logged)} ta dars qayd etilmagan — «📝 Yozilmagan darslar» orqali to'ldiring.")
    if overdue:
        tips.append("Muddati o'tgan vazifalarni birinchi navbatda yoping yoki qayta rejalashtiring.")
    if weak:
        tips.append(f"«{weak[0].topic}» mavzusini «📖 O'qish rejimi»da takrorlang.")
    if not tips:
        tips.append("Ajoyib hafta! Shu sur'atni saqlang. 💪")
    lines += ["", "💡 <b>Tavsiya</b>"] + [f"• {esc(t)}" for t in tips]
    return "\n".join(lines)


async def course_brief(session: AsyncSession, course: Course | None) -> str:
    """Short study-mode intro: what was covered recently in the chosen course."""
    if course is None:
        return "✨ <b>Erkin mavzu</b>\nNimani o'rganmoqchisiz? Savolingizni yozing yoki ovozli xabar yuboring."
    logs = await lessons.recent_logs(session, course.id, limit=3)
    lines = [f"📖 <b>O'qish rejimi: {esc(course.title)}</b>", ""]
    if logs:
        lines.append("<b>So'nggi darslar:</b>")
        lines += [f"• {timeutil.fmt_date(entry.class_date, False)} — {esc(truncate(entry.topic, 140))}" for entry in logs]
    else:
        lines.append("Bu fandan hali dars qaydi yo'q — umumiy dastur bo'yicha yordam beraman.")
    lines += ["", "Savol bering yoki quyidagilardan birini tanlang 👇"]
    return "\n".join(lines)

