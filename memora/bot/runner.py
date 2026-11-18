"""Telegram entry point (long polling)."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Dispatcher
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommand

from memora.bot.handlers import callbacks, menu, messages
from memora.bot.middleware import OwnerOnlyMiddleware, on_error

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query", "poll_answer"]
COMMANDS = [
    BotCommand(command="start", description="Menyuni ochish"),
    BotCommand(command="help", description="Yordam"),
    BotCommand(command="darslar", description="Yozilmagan darslar"),
    BotCommand(command="status", description="Tizim holati"),
    BotCommand(command="sync", description="Dars jadvalini yangilash"),
    BotCommand(command="pauza", description="Dars eslatmalarini to'xtatish (kun)"),
    BotCommand(command="davom", description="Eslatmalarni yoqish"),
    BotCommand(command="cancel", description="Joriy amalni bekor qilish"),
]


def build_dispatcher(app) -> Dispatcher:  # noqa: ANN001
    dispatcher = Dispatcher()
    dispatcher["app"] = app
    # Outer middleware on the root update observer: runs before filters, handlers and FSM.
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(app.settings.owner_chat_id))
    dispatcher.errors.register(on_error)
    dispatcher.include_router(menu.router)
    dispatcher.include_router(callbacks.router)
    dispatcher.include_router(messages.router)
    return dispatcher


async def run_bot(app) -> None:  # noqa: ANN001
    dispatcher = build_dispatcher(app)
    try:
        await app.bot.set_my_commands(COMMANDS)
    except Exception:  # noqa: BLE001 - cosmetic; never block startup on it
        log.warning("could not register bot commands", exc_info=True)
    log.info("bot polling started", extra={"owner": app.settings.owner_chat_id, "llm": app.llm.describe()})
    # Updates are handled as concurrent tasks, so a slow LLM turn never blocks a menu button.
    # Telegram being unreachable at start-up (or the connection dropping before polling is
    # established) is retried with backoff instead of killing the process; a rejected token
    # is fatal and surfaces immediately.
    delay = 2.0
    while True:
        try:
            await dispatcher.start_polling(
                app.bot, allowed_updates=ALLOWED_UPDATES, handle_signals=False, close_bot_session=False
            )
            return
        except TelegramNetworkError as exc:
            log.warning("telegram unreachable, retrying", extra={"error": str(exc)[:200], "retry_in": delay})
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
