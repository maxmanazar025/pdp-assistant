"""Inline-button handlers. See ``memora.ui`` for the callback-data protocol."""
from __future__ import annotations

import logging
from typing import Any

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from memora import flows, timeutil, ui
from memora.bot.handlers.menu import build_screen
from memora.db import session_scope
from memora.db.models import QuizItem, TimetableEntry
from memora.flows import Out, deliver
from memora.llm.client import LLMError
from memora.services import actions, lessons, quiz, reports
from memora.services import state as st
from memora.services import tasks as task_service
from memora.textfmt import esc

log = logging.getLogger(__name__)
router = Router(name="callbacks")


async def _edit(cq: CallbackQuery, text: str | None = None, markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit the message under the button; harmless failures (unchanged / too old) are ignored."""
    if not isinstance(cq.message, Message):
        return
    try:
        if text is None:
            await cq.message.edit_reply_markup(reply_markup=markup)
        else:
            await cq.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        log.debug("edit skipped", extra={"error": str(exc)})


def _original_html(cq: CallbackQuery) -> str:
    return cq.message.html_text if isinstance(cq.message, Message) and cq.message.text else ""


def _is_task_list(cq: CallbackQuery) -> bool:
    return isinstance(cq.message, Message) and (cq.message.text or "").startswith("📋 Vazifalarim")


@router.callback_query()
async def on_callback(cq: CallbackQuery, app) -> None:  # noqa: ANN001
    namespace, _, rest = (cq.data or "").partition(":")
    action, *args = rest.split(":")
    handler = _HANDLERS.get(namespace)
    if handler is None:
        await cq.answer("Noma'lum tugma", show_alert=False)
        return
    try:
        await handler(cq, app, action, args)
    except LLMError as exc:
        log.error("llm failed in callback", extra={"error": str(exc)[:300]})
        await app.notifier.alert_admin("LLM ishlamayapti", str(exc), window_minutes=30)
        await app.notifier.send(flows.LLM_DOWN)
    finally:
        try:
            await cq.answer()  # no-op if already answered; stops the button spinner
        except TelegramBadRequest:
            pass


# ------------------------------------------------------------------ m: menu screens
async def _menu(cq: CallbackQuery, app, action: str, args: list[str]) -> None:  # noqa: ANN001
    await cq.answer()
    if action == "review":
        outbox: list[Out] = []
        async with flows.turn_lock, flows.typing(app):
            async with session_scope() as session:
                await flows.start_quiz(session, app, None, outbox, review_first=True)
        await deliver(app, outbox)
        return
    await deliver(app, [await build_screen(app, action)])


# ------------------------------------------------------------------ t: tasks
async def _tasks(cq: CallbackQuery, app, action: str, args: list[str]) -> None:  # noqa: ANN001
    task_id = int(args[0])
    async with session_scope() as session:
        task = await task_service.get_task(session, task_id)
        if task is None or task.status == "deleted":
            await cq.answer("Bu vazifa topilmadi", show_alert=True)
            await _edit(cq, markup=None)
            return
        if action == "done":
            await task_service.complete_task(session, app, task)
            await cq.answer("✅ Bajarildi")
            if _is_task_list(cq):
                await session.flush()
                text, keyboard = await reports.render_tasks(session)
                await _edit(cq, text, keyboard)
            else:
                await _edit(cq, f"{_original_html(cq)}\n\n✅ <b>Bajarildi</b> — {timeutil.fmt_time(timeutil.now())}",
                            ui.task_done_kb(task.id))
        elif action == "undo":
            await task_service.reopen_task(session, app, task)
            await cq.answer("↩️ Qaytarildi")
            await _edit(cq, f"↩️ <b>Vazifa qayta ochildi:</b> {esc(task.title)}", ui.task_created_kb(task.id))
        elif action == "snz":
            minutes = int(args[1])
            if task.status != "pending":
                await cq.answer("Bu vazifa allaqachon yopilgan", show_alert=True)
                await _edit(cq, markup=None)
                return
            await task_service.snooze_task(session, app, task, minutes)
            await cq.answer(f"⏰ {timeutil.fmt_time(task.remind_at)} da eslataman")
            await _edit(cq, f"{_original_html(cq)}\n\n💤 <b>{timeutil.fmt_dt(task.remind_at)}</b> ga kechiktirildi", None)
        elif action == "del":
            pending = await actions.create_action(session, app, actions.TASK_DELETE,
                                                  {"task_id": task.id, "title": task.title})
            text, keyboard = await actions.render_card(session, app, pending)
            await st.set_state(session, app.chat_id, st.CONFIRMING, {"action_id": pending.id},
                               ttl_minutes=app.settings.action_ttl_minutes)
            await cq.answer()
            await session.commit()
            await app.notifier.send(text, keyboard)


# ------------------------------------------------------------------ a: human-in-the-loop
async def _actions(cq: CallbackQuery, app, action: str, args: list[str]) -> None:  # noqa: ANN001
    async with session_scope() as session:
        pending = await actions.get_action(session, args[0])
        if pending is None:
            await cq.answer("Bu so'rov topilmadi", show_alert=True)
            await _edit(cq, markup=None)
            return
        if action == "edit":
            out = await flows.begin_time_edit(session, app, pending)
            await cq.answer()
            await session.commit()
            await app.notifier.send(out.text, out.markup)
            return
        out = await flows.resolve_action(session, app, pending, confirm=(action == "ok"))
        still_pending = pending.status == "pending" and actions.is_live(pending)
    await cq.answer()
    if still_pending:  # execution failed (e.g. Google down): keep the card and its buttons
        await app.notifier.send(out.text)
    else:
        await _edit(cq, out.text, None)


# ------------------------------------------------------------------ c: classes
async def _classes(cq: CallbackQuery, app, action: str, args: list[str]) -> None:  # noqa: ANN001
    await cq.answer()
    if action == "prep":
        async with session_scope() as session:
            entry = await session.get(TimetableEntry, int(args[0]))
            if entry is None:
                await app.notifier.send("Bu dars jadvalda topilmadi.")
                return
            text, keyboard = await reports.render_prep(session, entry)
        await app.notifier.send(text, keyboard)
    elif action == "quiz":
        outbox: list[Out] = []
        async with flows.turn_lock, flows.typing(app):
            async with session_scope() as session:
                await flows.start_quiz(session, app, int(args[0]) or None, outbox)
        await deliver(app, outbox)


# ------------------------------------------------------------------ l: lesson reflection
async def _lessons(cq: CallbackQuery, app, action: str, args: list[str]) -> None:  # noqa: ANN001
    async with session_scope() as session:
        class_session = await lessons.get_session(session, int(args[0]))
        if class_session is None:
            await cq.answer("Dars topilmadi", show_alert=True)
            return
        state, ctx = await st.get_state(session, app.chat_id)
        awaiting_this = state == st.AWAIT_LOG and ctx.get("session_id") == class_session.id
        if action == "log":
            out = await flows.begin_lesson_log(session, app, class_session)
            await cq.answer()
            await session.commit()
            await app.notifier.send(out.text, out.markup)
        elif action == "later":
            if awaiting_this:
                await st.pop_state(session, app.chat_id)
            await cq.answer("Mayli, keyinroq 👌")
            await _edit(cq, f"{_original_html(cq)}\n\n⏭ <i>Keyinroq yoziladi — «📝 Yozilmagan darslar»da turadi.</i>", None)
        elif action == "none":
            if class_session.status == "pending":
                class_session.status = "cancelled"
            if awaiting_this:
                await st.pop_state(session, app.chat_id)
            await cq.answer("Belgilandi")
            await _edit(cq, f"🚫 <b>{esc(class_session.subject)}</b> — dars bo'lmadi deb belgilandi.", None)


# ------------------------------------------------------------------ s: study mode
async def _study(cq: CallbackQuery, app, action: str, args: list[str]) -> None:  # noqa: ANN001
    await cq.answer()
    outbox: list[Out] = []
    if action == "course":
        async with session_scope() as session:
            outbox.append(await flows.enter_study(session, app, int(args[0])))
        await _edit(cq, markup=None)
    elif action == "exit":
        async with session_scope() as session:
            outbox.append(await flows.exit_study(session, app))
    elif action in ("plan", "quiz", "cards"):
        async with flows.turn_lock, flows.typing(app):
            async with session_scope() as session:
                state, ctx = await st.get_state(session, app.chat_id)
                if action == "plan":
                    if state != st.STUDY:
                        outbox.append(await build_screen(app, "study"))
                    else:
                        await flows.study_turn(
                            session, app,
                            "So'nggi darslarim asosida shu fan bo'yicha bosqichma-bosqich o'quv reja tuzib ber: "
                            "har bosqich uchun vaqt va bitta amaliy mashq bilan.",
                            outbox,
                        )
                elif action == "quiz":
                    await flows.start_quiz(session, app, ctx.get("course_id") if state == st.STUDY else None, outbox,
                                           review_first=True)
                else:
                    await flows.next_flashcard(session, app, outbox)
    await deliver(app, outbox)


# ------------------------------------------------------------------ f: flashcards
async def _flashcards(cq: CallbackQuery, app, action: str, args: list[str]) -> None:  # noqa: ANN001
    if action == "stop":
        await cq.answer("To'xtatildi")
        await _edit(cq, markup=None)
        return
    outbox: list[Out] = []
    async with session_scope() as session:
        item = await session.get(QuizItem, int(args[0]))
        if item is None:
            await cq.answer("Kartochka topilmadi", show_alert=True)
            return
        if action == "show":
            await cq.answer()
            await _edit(cq, *flows.flashcard_back(item))
            return
        knew = args[1] == "1"
        await quiz.record_answer(session, item, knew, "flashcard")
        await cq.answer("✅ Zo'r!" if knew else "🔁 Yana qaytaramiz")
        text, _ = flows.flashcard_back(item)
        await _edit(cq, f"{text}\n\n{'✅ Bildim' if knew else '❌ Bilmadim — 10 daqiqadan keyin qaytadi'}", None)
        async with flows.turn_lock:
            await flows.next_flashcard(session, app, outbox)
    await deliver(app, outbox)


_HANDLERS: dict[str, Any] = {
    "m": _menu, "t": _tasks, "a": _actions, "c": _classes, "l": _lessons, "s": _study, "f": _flashcards,
}
