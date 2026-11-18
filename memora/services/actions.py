"""Human-in-the-loop pending actions.

Agents never mutate the calendar or delete tasks directly. They *propose*: a row is
stored here and the user gets a deterministic card with ✅ / ✏️ / ❌ buttons. Only the
button (or a typed "ha") executes the stored payload, so what the user confirmed is
exactly what runs — the LLM is out of the loop at execution time.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from memora import timeutil, ui
from memora.db.models import PendingAction
from memora.services import calendar as cal
from memora.services import tasks as task_service
from memora.textfmt import esc

EVENT_CREATE = "event_create"
EVENT_UPDATE = "event_update"
EVENT_DELETE = "event_delete"
TASK_DELETE = "task_delete"
TASK_CREATE = "task_create"
TIME_EDITABLE = {EVENT_CREATE, EVENT_UPDATE, TASK_CREATE}


def _dt(value: str | None) -> datetime | None:
    return timeutil.parse_iso_local(value) if value else None


async def create_action(session: AsyncSession, app, kind: str, payload: dict[str, Any]) -> PendingAction:  # noqa: ANN001
    action = PendingAction(
        id=secrets.token_hex(6), kind=kind, payload=payload, status="pending", created_at=timeutil.now(),
        expires_at=timeutil.now() + timedelta(minutes=app.settings.action_ttl_minutes),
    )
    session.add(action)
    await session.flush()
    return action


async def get_action(session: AsyncSession, action_id: str) -> PendingAction | None:
    return await session.get(PendingAction, action_id)


async def latest_pending(session: AsyncSession) -> PendingAction | None:
    result = await session.execute(
        select(PendingAction)
        .where(PendingAction.status == "pending", PendingAction.expires_at > timeutil.now())
        .order_by(PendingAction.created_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


def is_live(action: PendingAction) -> bool:
    return action.status == "pending" and action.expires_at > timeutil.now()


def _span(start: datetime, end: datetime) -> str:
    return f"{timeutil.fmt_dt(start)}–{timeutil.fmt_time(end)}"


async def render_card(session: AsyncSession, app, action: PendingAction) -> tuple[str, InlineKeyboardMarkup]:  # noqa: ANN001
    """Deterministic confirmation card (Uzbek HTML) with conflict warnings."""
    p = action.payload
    lines: list[str] = []
    conflicts: list[cal.Conflict] = []
    if action.kind == EVENT_CREATE:
        start, end = _dt(p["start"]), _dt(p["end"])
        lines += ["🗓 <b>Yangi tadbir qo'shilsinmi?</b>", "", f"<b>{esc(p['summary'])}</b>", f"🕒 {_span(start, end)}"]
        if p.get("location"):
            lines.append(f"📍 {esc(p['location'])}")
        if p.get("description"):
            lines.append(f"📝 {esc(p['description'])}")
        conflicts = await cal.find_conflicts(session, app, start, end)
    elif action.kind == EVENT_UPDATE:
        old_start, old_end = _dt(p["old_start"]), _dt(p["old_end"])
        start, end = _dt(p.get("start")) or old_start, _dt(p.get("end")) or old_end
        lines += ["✏️ <b>Tadbir o'zgartirilsinmi?</b>", "", f"<b>{esc(p.get('summary') or p['old_summary'])}</b>"]
        if p.get("summary") and p["summary"] != p["old_summary"]:
            lines.append(f"Nomi: {esc(p['old_summary'])} → <b>{esc(p['summary'])}</b>")
        if (start, end) != (old_start, old_end):
            lines.append(f"🕒 {_span(old_start, old_end)} → <b>{_span(start, end)}</b>")
        else:
            lines.append(f"🕒 {_span(start, end)}")
        if p.get("location"):
            lines.append(f"📍 <b>{esc(p['location'])}</b>")
        conflicts = await cal.find_conflicts(session, app, start, end, ignore_event_id=p["event_id"])
    elif action.kind == EVENT_DELETE:
        lines += ["🗑 <b>Tadbir o'chirilsinmi?</b>", "", f"<b>{esc(p['summary'])}</b>",
                  f"🕒 {_span(_dt(p['start']), _dt(p['end']))}"]
    elif action.kind == TASK_DELETE:
        lines += ["🗑 <b>Vazifa o'chirilsinmi?</b>", "", f"<b>{esc(p['title'])}</b>"]
    elif action.kind == TASK_CREATE:
        lines += ["📚 <b>Vazifa qo'shilsinmi?</b>", "", f"<b>{esc(p['title'])}</b>"]
        if p.get("due_at"):
            lines.append(f"📅 Muddat: {timeutil.fmt_dt(_dt(p['due_at']))}")
        if p.get("remind_at"):
            lines.append(f"⏰ Eslatma: {timeutil.fmt_dt(_dt(p['remind_at']))}")
    if conflicts:
        lines += ["", "⚠️ <b>Vaqt to'qnashuvi:</b>"]
        for c in conflicts[:5]:
            icon = "🎓" if c.kind == "class" else "🗓"
            lines.append(f"{icon} {timeutil.fmt_time(c.start)}–{timeutil.fmt_time(c.end)} · {esc(c.title)}")
    return "\n".join(lines), ui.action_kb(action.id, action.kind in TIME_EDITABLE)


def apply_new_time(action: PendingAction, new_start: datetime, has_time: bool = True) -> None:
    """Move the proposed start, preserving the duration ('✏️ Vaqtni o'zgartirish')."""
    p = dict(action.payload)
    if action.kind == EVENT_CREATE:
        duration = _dt(p["end"]) - _dt(p["start"])
        p["start"], p["end"] = timeutil.iso(new_start), timeutil.iso(new_start + duration)
    elif action.kind == EVENT_UPDATE:
        old_start = _dt(p.get("start")) or _dt(p["old_start"])
        old_end = _dt(p.get("end")) or _dt(p["old_end"])
        p["start"], p["end"] = timeutil.iso(new_start), timeutil.iso(new_start + (old_end - old_start))
    elif action.kind == TASK_CREATE:
        p["due_at"], p["remind_at"], p["has_time"] = timeutil.iso(new_start), timeutil.iso(new_start), has_time
    action.payload = p


async def execute(session: AsyncSession, app, action: PendingAction) -> str:  # noqa: ANN001
    """Run a confirmed action. Returns the Uzbek HTML result line."""
    p = action.payload
    if action.kind == EVENT_CREATE:
        event = await cal.create_event(
            session, app, p["summary"], _dt(p["start"]), _dt(p["end"]), p.get("location"), p.get("description")
        )
        text = f"✅ <b>Tadbir qo'shildi:</b> {esc(p['summary'])}\n🕒 {_span(_dt(p['start']), _dt(p['end']))}"
        if event.get("htmlLink"):
            text += f'\n<a href="{esc(event["htmlLink"])}">Google Calendar\'da ochish</a>'
    elif action.kind == EVENT_UPDATE:
        fields = {k: p[k] for k in ("summary", "location", "description") if p.get(k) is not None}
        if p.get("start"):
            fields["start"], fields["end"] = _dt(p["start"]), _dt(p["end"])
        await cal.update_event(session, app, p["event_id"], **fields)
        text = f"✅ <b>Tadbir yangilandi:</b> {esc(p.get('summary') or p['old_summary'])}"
        if p.get("start"):
            text += f"\n🕒 {_span(_dt(p['start']), _dt(p['end']))}"
    elif action.kind == EVENT_DELETE:
        await cal.delete_event(session, app, p["event_id"])
        text = f"🗑 <b>Tadbir o'chirildi:</b> {esc(p['summary'])}"
    elif action.kind == TASK_DELETE:
        task = await task_service.get_task(session, int(p["task_id"]))
        if task is not None and task.status != "deleted":
            await task_service.delete_task(session, app, task)
        text = f"🗑 <b>Vazifa o'chirildi:</b> {esc(p['title'])}"
    elif action.kind == TASK_CREATE:
        task = await task_service.create_task(
            session, app, p["title"], notes=p.get("notes"), due_at=_dt(p.get("due_at")),
            has_time=bool(p.get("has_time")), remind_at=_dt(p.get("remind_at")),
            source=p.get("source", "bot"), course_id=p.get("course_id"),
        )
        text = f"✅ <b>Vazifa qo'shildi:</b> {esc(task.title)}"
        if task.remind_at:
            text += f"\n⏰ Eslatma: {timeutil.fmt_dt(task.remind_at)}"
    else:
        raise ValueError(f"unknown action kind: {action.kind}")
    action.status = "confirmed"
    action.resolved_at = timeutil.now()
    return text


def cancel(action: PendingAction) -> None:
    action.status = "cancelled"
    action.resolved_at = timeutil.now()
