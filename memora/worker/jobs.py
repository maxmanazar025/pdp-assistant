"""Proactive lifecycle engine.

``tick`` is a pure function of (database state, current time): every run recomputes what
*should* have been sent by now and hands each candidate to ``Notifier.dispatch_once``
under a deterministic dedupe key. That makes the scheduler stateless — a restart, a
crash-loop or two overlapping workers cannot produce a duplicate, and alerts missed while
the container was down are caught up within their grace window.

Dedupe keys
-----------
briefing:<date>                      class_alert:<date>:<entry_id>
class_end:<date>:<entry_id>          task:<task_id>:<remind_at epoch>
weekly:<iso-year>-W<week>            review:<date>
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update

from memora import timeutil, ui
from memora.db import kv_get, kv_set, session_scope
from memora.db.models import ClassSession, Notification, PendingAction, QuizPoll, Task
from memora.integrations.google import GoogleAuthError, GoogleError
from memora.services import calendar as cal
from memora.services import lessons, quiz, reports
from memora.services import state as st
from memora.services import tasks as task_service
from memora.services.notify import MAX_ATTEMPTS
from memora.timetable import service as timetable

log = logging.getLogger(__name__)

BRIEFING_CUTOFF_HOUR = 12        # a briefing missed during downtime is still sent before noon
CLASS_END_GRACE = timedelta(minutes=90)
REVIEW_MIN_CARDS = 3


async def _guard(app, name: str, coro) -> None:  # noqa: ANN001
    """Isolate sub-jobs: one failing job never starves the others."""
    try:
        await coro
    except Exception as exc:  # noqa: BLE001
        log.exception("job failed", extra={"job": name})
        await app.notifier.alert_admin(f"Fon vazifasi xatosi: {name}", exc=exc)


async def tick(app, now: datetime | None = None) -> None:  # noqa: ANN001
    now = timeutil.local(now) if now else timeutil.now()
    async with session_scope() as session:
        await kv_set(session, "worker:heartbeat", timeutil.iso(timeutil.now()))
        mute_until = await kv_get(session, "mute_until")
    muted = bool(mute_until) and now < timeutil.parse_iso_local(mute_until)

    if not muted:
        await _guard(app, "briefing", morning_briefing(app, now))
        await _guard(app, "class_alerts", class_alerts(app, now))
        await _guard(app, "class_end", class_end_prompts(app, now))
        await _guard(app, "review_nudge", review_nudge(app, now))
    await _guard(app, "task_reminders", task_reminders(app, now))
    await _guard(app, "weekly_digest", weekly_digest(app, now))


# ------------------------------------------------------------------ 07:30 briefing
async def morning_briefing(app, now: datetime) -> None:  # noqa: ANN001
    if not (app.settings.briefing_at <= now.time() and now.hour < BRIEFING_CUTOFF_HOUR):
        return

    async def send():  # noqa: ANN202
        async with session_scope() as session:
            try:
                await cal.refresh_cache(session, app)  # freshest calendar for the day's digest
            except GoogleError as exc:
                log.warning("briefing uses cached calendar", extra={"error": str(exc)[:200]})
        async with session_scope() as session:
            text, keyboard = await reports.render_briefing(session, app)
        return await app.notifier.send(text, keyboard)

    await app.notifier.dispatch_once(f"briefing:{now.date().isoformat()}", "briefing", send)


# ------------------------------------------------------------------ T-15 class alerts
async def class_alerts(app, now: datetime) -> None:  # noqa: ANN001
    lead = timedelta(minutes=app.settings.class_alert_minutes)
    async with session_scope() as session:
        entries = await timetable.classes_on(session, now.date())
    for entry in entries:
        start = timeutil.combine(now.date(), entry.start_time)
        if not (start - lead <= now < start):
            continue
        minutes_left = max(1, math.ceil((start - now).total_seconds() / 60))

        async def send(entry=entry, minutes_left=minutes_left):  # noqa: ANN001, ANN202
            return await app.notifier.send(reports.render_class_alert(entry, minutes_left), ui.class_alert_kb(entry.id))

        await app.notifier.dispatch_once(f"class_alert:{now.date().isoformat()}:{entry.id}", "class_alert", send)


# ------------------------------------------------------------------ post-class reflection
async def class_end_prompts(app, now: datetime) -> None:  # noqa: ANN001
    async with session_scope() as session:
        entries = await timetable.classes_on(session, now.date())
    for entry in entries:
        end = timeutil.combine(now.date(), entry.end_time)
        if not (end <= now < end + CLASS_END_GRACE):
            continue
        if timetable.continues_same_course(entry, entries):
            continue  # double period: ask once, after the last slot

        async def send(entry=entry):  # noqa: ANN001, ANN202
            async with session_scope() as session:
                class_session = await lessons.open_session(session, entry, now.date())
                if class_session.status != "pending":
                    return None
                session_id = class_session.id
                prompt = reports.render_lesson_prompt(class_session)
            message = await app.notifier.send(prompt, ui.lesson_prompt_kb(session_id))
            async with session_scope() as session:
                await session.execute(
                    update(ClassSession).where(ClassSession.id == session_id)
                    .values(prompt_message_id=message.message_id)
                )
                await st.set_state(session, app.chat_id, st.AWAIT_LOG, {"session_id": session_id},
                                   ttl_minutes=app.settings.lesson_log_timeout_minutes)
            return message

        await app.notifier.dispatch_once(f"class_end:{now.date().isoformat()}:{entry.id}", "class_end", send)


# ------------------------------------------------------------------ due-task reminders
async def task_reminders(app, now: datetime) -> None:  # noqa: ANN001
    async with session_scope() as session:
        due = [(t.id, t.remind_at) for t in await task_service.due_reminders(session, now)]
    for task_id, remind_at in due:
        key = f"task:{task_id}:{int(remind_at.timestamp())}"

        async def send(task_id=task_id):  # noqa: ANN001, ANN202
            async with session_scope() as session:
                task = await session.get(Task, task_id)
                if task is None or task.status != "pending":
                    return None
                text = reports.render_reminder(task)
            return await app.notifier.send(text, ui.reminder_kb(task_id))

        await app.notifier.dispatch_once(key, "task_reminder", send)
        async with session_scope() as session:
            ledger = (
                await session.execute(select(Notification).where(Notification.dedupe_key == key))
            ).scalar_one_or_none()
            settled = ledger is not None and (ledger.status == "sent" or ledger.attempts >= MAX_ATTEMPTS)
            if settled:  # stop re-evaluating this reminder; a snooze sets a new remind_at
                await session.execute(
                    update(Task).where(Task.id == task_id, Task.remind_at == remind_at).values(reminded_at=timeutil.now())
                )


# ------------------------------------------------------------------ Sunday digest
async def weekly_digest(app, now: datetime) -> None:  # noqa: ANN001
    if now.weekday() != 6 or now.time() < app.settings.weekly_digest_at:
        return
    iso_year, iso_week, _ = now.isocalendar()

    async def send():  # noqa: ANN202
        async with session_scope() as session:
            text = await reports.render_weekly(session, now.date())
        return await app.notifier.send(text)

    await app.notifier.dispatch_once(f"weekly:{iso_year}-W{iso_week:02d}", "weekly_digest", send)


# ------------------------------------------------------------------ spaced-repetition nudge
async def review_nudge(app, now: datetime) -> None:  # noqa: ANN001
    at = app.settings.review_nudge_at
    if at is None or now.time() < at or now.hour >= 23:
        return
    async with session_scope() as session:
        count = await quiz.due_count(session)
    if count < REVIEW_MIN_CARDS:
        return

    async def send():  # noqa: ANN202
        return await app.notifier.send(
            f"🧠 <b>Takrorlash vaqti</b>\nBugun <b>{count} ta</b> savol takrorlashni kutmoqda — 5 daqiqa kifoya.",
            ui.review_nudge_kb(),
        )

    await app.notifier.dispatch_once(f"review:{now.date().isoformat()}", "review_nudge", send)


# ------------------------------------------------------------------ Google sync
async def google_sync(app) -> None:  # noqa: ANN001
    """Push the task outbox, pull remote task changes, refresh the calendar cache."""
    if not app.google.enabled:
        return
    error: str | None = None
    try:
        async with session_scope() as session:
            _, failure = await task_service.push_pending(session, app)
        if failure is not None:
            raise failure
        async with session_scope() as session:
            await task_service.pull_google(session, app)
        async with session_scope() as session:
            await cal.refresh_cache(session, app)
    except GoogleAuthError as exc:
        error = str(exc)
        await app.notifier.alert_admin(
            "Google avtorizatsiyasi ishlamayapti",
            f"{exc}\nscripts/google_auth.py orqali yangi GOOGLE_REFRESH_TOKEN oling va konteynerni qayta ishga tushiring.",
            window_minutes=360,
        )
    except GoogleError as exc:
        error = str(exc)
        log.warning("google sync failed", extra={"error": error[:300]})
    async with session_scope() as session:
        previous = await kv_get(session, "google:last_sync") or {}
        streak = (previous.get("fail_streak", 0) + 1) if error else 0
        await kv_set(session, "google:last_sync", {"at": timeutil.iso(timeutil.now()), "error": error,
                                                   "fail_streak": streak})
    if error and streak == 5:  # ~15 minutes of consecutive failures
        await app.notifier.alert_admin("Google sinxronlash ketma-ket ishlamayapti", error, window_minutes=360)


# ------------------------------------------------------------------ housekeeping
async def maintenance(app) -> None:  # noqa: ANN001
    now = timeutil.now()
    async with session_scope() as session:
        await st.prune_memory(session)
        await session.execute(
            update(PendingAction).where(PendingAction.status == "pending", PendingAction.expires_at < now)
            .values(status="expired", resolved_at=now)
        )
        await session.execute(delete(Notification).where(Notification.created_at < now - timedelta(days=60)))
        await session.execute(delete(QuizPoll).where(QuizPoll.sent_at < now - timedelta(days=30)))
        await session.execute(delete(PendingAction).where(PendingAction.created_at < now - timedelta(days=30)))
