"""Outbound Telegram delivery: safe HTML sending, idempotent dispatch, admin diagnostics."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import traceback
from datetime import datetime, timezone
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message, ReplyKeyboardMarkup
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from memora.db import session_scope
from memora.db.models import Notification
from memora.textfmt import esc, render_markdown, split_html, strip_tags, truncate

log = logging.getLogger(__name__)

Markup = InlineKeyboardMarkup | ReplyKeyboardMarkup | None
MAX_ATTEMPTS = 6
RETRY_BASE_SECONDS = 30  # failed sends are retried after attempts² × 30 s: 30 s, 2 min, 4.5, 8, 12.5


class Notifier:
    def __init__(self, bot: Bot, chat_id: int) -> None:
        self.bot = bot
        self.chat_id = chat_id

    # ------------------------------------------------------------------ sending
    async def _send_one(self, text: str, reply_markup: Markup, silent: bool) -> Message:
        for attempt in range(3):
            try:
                return await self.bot.send_message(
                    self.chat_id, text, reply_markup=reply_markup, disable_notification=silent
                )
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(exc.retry_after, 30) + 0.5)
            except TelegramBadRequest as exc:
                if "parse" not in str(exc).lower() and "entit" not in str(exc).lower():
                    raise
                # Last line of defence: never lose a message to a markup problem.
                log.warning("telegram rejected html, falling back to plain text", extra={"error": str(exc)})
                return await self.bot.send_message(
                    self.chat_id, strip_tags(text), reply_markup=reply_markup,
                    disable_notification=silent, parse_mode=None,
                )
        return await self.bot.send_message(self.chat_id, text, reply_markup=reply_markup, disable_notification=silent)

    async def send(self, html_text: str, reply_markup: Markup = None, silent: bool = False) -> Message:
        """Send code-generated HTML. The keyboard is attached to the last part."""
        parts = split_html(html_text)
        message: Message | None = None
        for index, part in enumerate(parts):
            message = await self._send_one(part, reply_markup if index == len(parts) - 1 else None, silent)
        assert message is not None
        return message

    async def send_markdown(self, markdown: str, reply_markup: Markup = None) -> Message | None:
        """Send LLM-authored markdown, converted to safe Telegram HTML."""
        parts = render_markdown(markdown)
        message: Message | None = None
        for index, part in enumerate(parts):
            message = await self._send_one(part, reply_markup if index == len(parts) - 1 else None, False)
        return message

    # ------------------------------------------------------------------ idempotent dispatch
    async def dispatch_once(self, key: str, kind: str, send: Callable[[], Awaitable[Message | None]]) -> bool:
        """Run ``send`` only if ``key`` has never been dispatched.

        The claim row is committed *before* the network call, so a crash or container
        restart at any point can never cause the same alert to go out twice (at-most-once
        across crashes). A send that raised is marked ``failed`` and re-claimed by later
        ticks with quadratic backoff, up to MAX_ATTEMPTS, which rides out short Telegram
        or network outages.
        """
        async with session_scope() as session:
            claimed = (
                await session.execute(
                    pg_insert(Notification)
                    .values(dedupe_key=key, kind=kind, status="claimed")
                    .on_conflict_do_nothing(index_elements=[Notification.dedupe_key])
                    .returning(Notification.id)
                )
            ).scalar_one_or_none()
            if claimed is None:
                claimed = (
                    await session.execute(
                        update(Notification)
                        .where(
                            Notification.dedupe_key == key,
                            Notification.status == "failed",
                            Notification.attempts < MAX_ATTEMPTS,
                            func.extract("epoch", func.now() - Notification.created_at)
                            >= Notification.attempts * Notification.attempts * RETRY_BASE_SECONDS,
                        )
                        .values(status="claimed", attempts=Notification.attempts + 1)
                        .returning(Notification.id)
                    )
                ).scalar_one_or_none()
        if claimed is None:
            return False
        try:
            message = await send()
        except Exception as exc:  # noqa: BLE001 - recorded, retried by a later tick
            log.exception("notification failed", extra={"key": key})
            async with session_scope() as session:
                await session.execute(
                    update(Notification).where(Notification.id == claimed)
                    .values(status="failed", error=truncate(f"{type(exc).__name__}: {exc}", 500))
                )
            return False
        async with session_scope() as session:
            await session.execute(
                update(Notification).where(Notification.id == claimed).values(
                    status="sent", sent_at=datetime.now(timezone.utc),
                    tg_message_id=message.message_id if message else None, error=None,
                )
            )
        return True

    # ------------------------------------------------------------------ self-monitoring
    async def alert_admin(self, title: str, detail: str = "", exc: BaseException | None = None,
                          window_minutes: int = 15) -> None:
        """Diagnostic alert to the owner, rate-limited per (title, error type) window."""
        try:
            signature = f"{title}|{type(exc).__name__ if exc else detail[:80]}"
            bucket = int(datetime.now(timezone.utc).timestamp() // (window_minutes * 60))
            key = f"diag:{hashlib.sha1(signature.encode()).hexdigest()[:16]}:{bucket}"
            lines = [f"🚨 <b>Memora OS: {esc(title)}</b>"]
            if detail:
                lines.append(esc(truncate(detail, 700)))
            if exc is not None:
                tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                lines.append(f"<pre>{esc(tb[-1800:])}</pre>")
            lines.append(f"<i>{datetime.now(timezone.utc).isoformat(timespec='seconds')}</i>")
            await self.dispatch_once(key, "diagnostic", lambda: self.send("\n".join(lines), silent=True))
        except Exception:  # noqa: BLE001 - monitoring must never raise into callers
            log.exception("failed to deliver diagnostic alert")
