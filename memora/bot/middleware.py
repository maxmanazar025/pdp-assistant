"""Security and resilience middleware."""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import ErrorEvent, TelegramObject, Update

log = logging.getLogger(__name__)


def _actor_ids(update: Update) -> tuple[int | None, int | None]:
    """(user_id, chat_id) for every update type the bot subscribes to."""
    if update.message:
        return (update.message.from_user.id if update.message.from_user else None, update.message.chat.id)
    if update.edited_message:
        msg = update.edited_message
        return (msg.from_user.id if msg.from_user else None, msg.chat.id)
    if update.callback_query:
        cq = update.callback_query
        return (cq.from_user.id, cq.message.chat.id if cq.message else cq.from_user.id)
    if update.poll_answer:
        user = update.poll_answer.user
        return (user.id if user else None, user.id if user else None)
    return (None, None)


class OwnerOnlyMiddleware(BaseMiddleware):
    """Single-user whitelist enforced before any handler, filter or FSM code runs.

    Updates from any other user or chat are dropped silently: no reply, no state, no LLM
    tokens. Both the sender and the chat must be the owner's private chat, so adding the
    bot to a group gives nobody access.
    """

    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        if isinstance(event, Update):
            user_id, chat_id = _actor_ids(event)
            if user_id != self.owner_id or chat_id != self.owner_id:
                log.warning("unauthorized update dropped", extra={
                    "user_id": user_id, "chat_id": chat_id, "update_type": event.event_type,
                })
                return None
        started = time.perf_counter()
        try:
            return await handler(event, data)
        finally:
            if isinstance(event, Update):
                log.info("update handled", extra={
                    "update_type": event.event_type, "ms": round((time.perf_counter() - started) * 1000),
                })


async def on_error(event: ErrorEvent, app) -> bool:  # noqa: ANN001
    """Last-resort handler: log, tell the user something went wrong, alert the owner with details."""
    log.exception("unhandled error in handler", exc_info=event.exception)
    try:
        await app.notifier.send("⚠️ Kutilmagan xatolik yuz berdi. Tafsilotlar qayd etildi — qayta urinib ko'ring.")
    except Exception:  # noqa: BLE001
        log.exception("could not notify user about the error")
    await app.notifier.alert_admin("Bot ishlov berishda xatolik", exc=event.exception)
    return True
