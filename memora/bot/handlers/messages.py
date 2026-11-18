"""Free-form input: text, voice notes and quiz-poll answers."""
from __future__ import annotations

import io
import logging

from aiogram import F, Router
from aiogram.types import Message, PollAnswer

from memora import flows
from memora.db import session_scope
from memora.integrations.stt import STTError
from memora.services import quiz
from memora.textfmt import esc, truncate

log = logging.getLogger(__name__)
router = Router(name="messages")

MAX_AUDIO_BYTES = 20 * 1024 * 1024  # Bot API getFile limit


def _reply_id(message: Message) -> int | None:
    return message.reply_to_message.message_id if message.reply_to_message else None


@router.message(F.voice | F.audio | F.video_note)
async def on_voice(message: Message, app) -> None:  # noqa: ANN001
    """Voice-to-action: transcribe, echo the transcript, then treat it exactly like typed text."""
    media = message.voice or message.audio or message.video_note
    if (media.file_size or 0) > MAX_AUDIO_BYTES:
        await app.notifier.send("🎙 Ovozli xabar juda katta (20 MB dan oshmasin).")
        return
    async with flows.typing(app):
        try:
            buffer = io.BytesIO()
            await app.bot.download(media.file_id, destination=buffer)
            transcript = await app.stt.transcribe(buffer.getvalue())
        except STTError as exc:
            log.warning("transcription failed", extra={"error": str(exc)[:300]})
            await app.notifier.alert_admin("Ovozni matnga o'girishda xatolik", str(exc), window_minutes=30)
            await app.notifier.send("🎙 Ovozli xabarni matnga o'gira olmadim. Iltimos, yozib yuboring.")
            return
    if not transcript:
        await app.notifier.send("🎙 Ovozli xabarda nutq eshitilmadi. Qaytadan urinib ko'ring.")
        return
    await app.notifier.send(f"🎙 <i>{esc(truncate(transcript, 1500))}</i>", silent=True)
    await flows.handle_text(app, transcript, input_kind="voice", reply_to_message_id=_reply_id(message))


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, app) -> None:  # noqa: ANN001
    await flows.handle_text(app, message.text, input_kind="text", reply_to_message_id=_reply_id(message))


@router.message(F.text.startswith("/"))
async def on_unknown_command(message: Message, app) -> None:  # noqa: ANN001
    await app.notifier.send("Bunday buyruq yo'q. Yordam: /help")


@router.message()
async def on_other(message: Message, app) -> None:  # noqa: ANN001
    caption = (message.caption or "").strip()
    if caption:
        await flows.handle_text(app, caption, input_kind="text", reply_to_message_id=_reply_id(message))
    else:
        await app.notifier.send("Hozircha faqat matn va ovozli xabarlarni tushunaman. 🙂")


@router.poll_answer()
async def on_poll_answer(answer: PollAnswer, app) -> None:  # noqa: ANN001
    async with session_scope() as session:
        summary = await quiz.handle_poll_answer(session, answer.poll_id, list(answer.option_ids))
    if summary is not None:
        await flows.deliver(app, [flows.quiz_summary(summary)])
