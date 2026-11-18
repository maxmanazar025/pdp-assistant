"""Application container: one place that wires configuration and long-lived clients."""
from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode

from memora.config import Settings, get_settings
from memora.integrations.google import GoogleClient
from memora.integrations.stt import Transcriber
from memora.llm.client import LLMRouter
from memora.services.notify import Notifier


@dataclass(slots=True)
class App:
    settings: Settings
    bot: Bot
    llm: LLMRouter
    google: GoogleClient
    stt: Transcriber
    notifier: Notifier

    @property
    def chat_id(self) -> int:
        return self.settings.owner_chat_id

    async def aclose(self) -> None:
        await self.llm.aclose()
        await self.google.aclose()
        await self.stt.aclose()
        await self.bot.session.close()


def build_app(settings: Settings | None = None, *, bot_session: BaseSession | None = None) -> App:
    settings = settings or get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    bot = Bot(
        token=settings.telegram_bot_token,
        session=bot_session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    return App(
        settings=settings,
        bot=bot,
        llm=LLMRouter(settings),
        google=GoogleClient(settings),
        stt=Transcriber(settings),
        notifier=Notifier(bot, settings.owner_chat_id),
    )
