"""Conversation state machine and per-domain episodic memory (both persisted in Postgres).

States
------
idle                  nothing pending; free text goes through the router
study_mode            Study Coach session; text goes straight to the coach
awaiting_lesson_log   a class just ended; the next text/voice is the lesson report
confirming_action     a HITL card is on screen; "ha"/"yo'q" resolve it
awaiting_time_edit    user pressed "✏️ Vaqtni o'zgartirish"; next text is the new time

Interrupting states (lesson log, confirmation, time edit) remember the state they
interrupted and restore it when they finish or expire, so a class ending in the middle
of a study session does not throw the session away.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from memora import timeutil
from memora.db.models import ConversationState, MemoryMessage

IDLE = "idle"
STUDY = "study_mode"
AWAIT_LOG = "awaiting_lesson_log"
CONFIRMING = "confirming_action"
AWAIT_TIME = "awaiting_time_edit"
INTERRUPTING = {AWAIT_LOG, CONFIRMING, AWAIT_TIME}

MEMORY_TURNS = 8
MEMORY_MAX_AGE = timedelta(hours=6)
MEMORY_RETENTION = timedelta(days=14)


def _utcnow() -> datetime:
    return timeutil.now()  # single clock source for the whole app (aware datetime)


async def _write(session: AsyncSession, chat_id: int, state: str, context: dict[str, Any],
                 expires_at: datetime | None) -> None:
    stmt = pg_insert(ConversationState).values(
        chat_id=chat_id, state=state, context=context, expires_at=expires_at, updated_at=_utcnow()
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ConversationState.chat_id],
        set_={"state": state, "context": context, "expires_at": expires_at, "updated_at": _utcnow()},
    )
    await session.execute(stmt)


async def get_state(session: AsyncSession, chat_id: int) -> tuple[str, dict[str, Any]]:
    """Current (state, context); expired states are unwound lazily."""
    row = (
        await session.execute(select(ConversationState).where(ConversationState.chat_id == chat_id))
    ).scalar_one_or_none()
    if row is None:
        return IDLE, {}
    if row.expires_at is not None and row.expires_at <= _utcnow():
        return await pop_state(session, chat_id)
    return row.state, dict(row.context or {})


async def set_state(session: AsyncSession, chat_id: int, state: str, context: dict[str, Any] | None = None,
                    ttl_minutes: int | None = None) -> None:
    """Enter ``state``. Interrupting states stack on top of the state they interrupt."""
    context = dict(context or {})
    if state in INTERRUPTING:
        current, current_ctx = await get_state(session, chat_id)
        if current in INTERRUPTING:  # replace the interruption, keep what it had interrupted
            previous = current_ctx.get("_prev")
        elif current != IDLE:
            previous = {"state": current, "context": current_ctx}
        else:
            previous = None
        if previous:
            context["_prev"] = previous
    expires = _utcnow() + timedelta(minutes=ttl_minutes) if ttl_minutes else None
    await _write(session, chat_id, state, context, expires)


async def update_context(session: AsyncSession, chat_id: int, **changes: Any) -> None:
    row = (
        await session.execute(select(ConversationState).where(ConversationState.chat_id == chat_id))
    ).scalar_one_or_none()
    if row is not None:
        row.context = {**(row.context or {}), **changes}


async def pop_state(session: AsyncSession, chat_id: int) -> tuple[str, dict[str, Any]]:
    """Leave the current state, returning to whatever it interrupted (or idle)."""
    row = (
        await session.execute(select(ConversationState).where(ConversationState.chat_id == chat_id))
    ).scalar_one_or_none()
    previous = (row.context or {}).get("_prev") if row else None
    if previous:
        await _write(session, chat_id, previous["state"], previous.get("context") or {}, None)
        return previous["state"], previous.get("context") or {}
    await _write(session, chat_id, IDLE, {}, None)
    return IDLE, {}


async def reset_state(session: AsyncSession, chat_id: int) -> None:
    await _write(session, chat_id, IDLE, {}, None)


# ------------------------------------------------------------------------- episodic memory
async def remember(session: AsyncSession, domain: str, role: str, content: str) -> None:
    session.add(MemoryMessage(domain=domain, role=role, content=content[:6000], created_at=_utcnow()))
    await session.flush()


async def recall(session: AsyncSession, domain: str, limit: int = MEMORY_TURNS) -> list[dict[str, str]]:
    """Most recent turns of one domain, oldest first, in chat-completions format."""
    rows = (
        await session.execute(
            select(MemoryMessage)
            .where(MemoryMessage.domain == domain, MemoryMessage.created_at >= _utcnow() - MEMORY_MAX_AGE)
            .order_by(MemoryMessage.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [{"role": r.role, "content": r.content} for r in reversed(rows)]


async def last_domain(session: AsyncSession) -> str | None:
    """Domain of the latest remembered turn (lets the router resolve follow-ups)."""
    row = (
        await session.execute(
            select(MemoryMessage.domain)
            .where(MemoryMessage.created_at >= _utcnow() - timedelta(minutes=20))
            .order_by(MemoryMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def prune_memory(session: AsyncSession) -> None:
    await session.execute(delete(MemoryMessage).where(MemoryMessage.created_at < _utcnow() - MEMORY_RETENTION))
