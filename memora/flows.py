"""Conversation flows shared by text, voice and button handlers.

Every flow follows the same discipline: do all database work inside one transaction,
collect outgoing messages in an *outbox*, commit, and only then talk to Telegram. A failed
send can therefore never roll back state that already reached Google, and a failed
transaction never leaves the user with a message describing something that did not happen.
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from memora import timeutil, ui
from memora.agents import router
from memora.agents.base import ToolContext, run_agent
from memora.agents.reflection import extract_reflection, resolve_time_llm
from memora.agents.specs import AGENTS, STUDY_AGENT
from memora.db import session_scope
from memora.db.models import ClassSession, Course, PendingAction, QuizItem, StudySession
from memora.integrations.google import GoogleError
from memora.llm.client import LLMError
from memora.services import actions, lessons, quiz, reports
from memora.services import state as st
from memora.textfmt import esc, truncate
from memora.timeparse import parse_when, translit
from memora.timetable import service as timetable

log = logging.getLogger(__name__)

# One conversational turn at a time keeps state transitions and memory strictly ordered.
# Fast-path menu buttons do not take this lock, so they stay instant during an LLM call.
turn_lock = asyncio.Lock()

_YES = {"ha", "xa", "xop", "hop", "mayli", "tasdiqla", "tasdiqlayman", "ok", "okay", "yes", "da", "albatta",
        "bo'pti", "to'g'ri", "shunday"}
_NO = {"yo'q", "yoq", "bekor", "bekor qil", "no", "net", "kerak emas", "kerakmas", "shart emas", "otmena"}

LLM_DOWN = ("😔 Hozir AI xizmati javob bermayapti. Birozdan keyin qayta urinib ko'ring — "
            "menyu tugmalari, jadval va eslatmalar ishlashda davom etadi.")


@dataclass(slots=True)
class Out:
    text: str
    markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None
    markdown: bool = False


async def deliver(app, outbox: list[Out]) -> None:  # noqa: ANN001
    for out in outbox:
        if out.markdown:
            await app.notifier.send_markdown(out.text, out.markup)
        else:
            await app.notifier.send(out.text, out.markup)


@asynccontextmanager
async def typing(app) -> AsyncIterator[None]:  # noqa: ANN001
    """Keep the 'typing…' indicator alive while a slow (LLM) operation runs."""
    async def pump() -> None:
        try:
            while True:
                await app.bot.send_chat_action(app.chat_id, "typing")
                await asyncio.sleep(4.5)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - cosmetic only
            return

    task = asyncio.create_task(pump())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _norm(text: str) -> str:
    return re.sub(r"[^\w' ]+", "", translit(text)).strip()


# ============================================================================ HITL actions
async def resolve_action(session: AsyncSession, app, action: PendingAction, confirm: bool) -> Out:  # noqa: ANN001
    """Confirm or cancel a pending action and unwind the confirmation state."""
    if not actions.is_live(action):
        if action.status == "pending":
            action.status = "expired"
        result = Out("⌛️ Bu so'rov eskirgan yoki allaqachon bajarilgan. Kerak bo'lsa, qaytadan yozing.")
    elif not confirm:
        actions.cancel(action)
        result = Out("❌ Bekor qilindi.")
    else:
        try:
            async with session.begin_nested():
                result = Out(await actions.execute(session, app, action))
        except GoogleError as exc:
            log.warning("action execution failed", extra={"action": action.id, "error": str(exc)})
            return Out("⚠️ Google bilan bog'lanib bo'lmadi, o'zgarish <b>kiritilmadi</b>. "
                       "Birozdan keyin «✅ Tasdiqlash»ni qayta bosing.")
    state, ctx = await st.get_state(session, app.chat_id)
    if state in (st.CONFIRMING, st.AWAIT_TIME) and ctx.get("action_id") == action.id:
        await st.pop_state(session, app.chat_id)
    return result


async def begin_time_edit(session: AsyncSession, app, action: PendingAction) -> Out:  # noqa: ANN001
    if not actions.is_live(action):
        return Out("⌛️ Bu so'rov eskirgan. Kerak bo'lsa, qaytadan yozing.")
    await st.set_state(session, app.chat_id, st.AWAIT_TIME, {"action_id": action.id},
                       ttl_minutes=app.settings.action_ttl_minutes)
    return Out("✏️ Yangi vaqtni yozing yoki ayting.\n<i>Masalan: «ertaga 15:00», «juma kuni soat 4 da», "
               "«2 soatdan keyin»</i>")


async def _handle_time_edit(session: AsyncSession, app, text: str, ctx: dict, outbox: list[Out]) -> None:  # noqa: ANN001
    action = await actions.get_action(session, ctx.get("action_id", ""))
    if action is None or not actions.is_live(action):
        await st.pop_state(session, app.chat_id)
        outbox.append(Out("⌛️ Bu so'rov eskirgan. Kerak bo'lsa, qaytadan yozing."))
        return
    payload = action.payload
    original = timeutil.parse_iso_local(
        payload.get("start") or payload.get("old_start") or payload.get("due_at") or timeutil.iso(timeutil.now())
    )
    parsed = parse_when(text)
    if parsed is not None:
        new_start = parsed.when
        if not parsed.has_time:  # only the day changed: keep the original clock time
            new_start = timeutil.combine(parsed.when.date(), original.time())
    else:
        new_start = await resolve_time_llm(app, text)
    if new_start is None:
        outbox.append(Out("🤔 Vaqtni tushunmadim. Masalan: «ertaga 15:00» yoki «juma 10:30» deb yozing."))
        return
    if new_start <= timeutil.now():
        outbox.append(Out("⏪ Bu vaqt allaqachon o'tib ketgan. Kelajakdagi vaqtni yozing."))
        return
    actions.apply_new_time(action, new_start)
    await st.set_state(session, app.chat_id, st.CONFIRMING, {"action_id": action.id},
                       ttl_minutes=app.settings.action_ttl_minutes)
    text_html, keyboard = await actions.render_card(session, app, action)
    outbox.append(Out(text_html, keyboard))


# ============================================================================ lesson reflection
async def log_lesson(session: AsyncSession, app, class_session: ClassSession, text: str, input_kind: str,  # noqa: ANN001
                     outbox: list[Out]) -> bool:
    """Reflection pipeline: extract -> save -> homework task. False if the text was off-topic."""
    reflection = await extract_reflection(app, text, class_session.subject)
    if not reflection.relevant:
        return False
    entry = await lessons.save_log(
        session, course_id=class_session.course_id, class_date=class_session.class_date,
        topic=reflection.topic, homework=reflection.homework, raw_text=text, input_kind=input_kind,
        class_session=class_session,
    )
    lines = [
        f"📝 <b>Qayd etildi</b> — {esc(class_session.subject)}",
        f"📖 <b>Mavzu:</b> {esc(entry.topic)}",
        f"📚 <b>Uy vazifasi:</b> {esc(entry.homework) if entry.homework else 'berilmagan'}",
    ]
    if reflection.degraded:
        lines.append("<i>AI vaqtincha ishlamadi — matningiz o'z holicha saqlandi.</i>")
    outbox.append(Out("\n".join(lines)))
    homework = await lessons.schedule_homework(session, app, entry)
    if homework is not None:
        outbox.append(Out(*homework))

    state, ctx = await st.get_state(session, app.chat_id)
    if state == st.AWAIT_LOG and ctx.get("session_id") == class_session.id:
        await st.pop_state(session, app.chat_id)
    remaining = [s for s in await lessons.pending_sessions(session) if s.id != class_session.id]
    if remaining:
        outbox.append(Out(f"Yana <b>{len(remaining)} ta</b> dars yozilmagan.",
                          ui.pending_lessons_kb([(s.id, truncate(
                              f"{timeutil.fmt_day_label(s.class_date)} {timeutil.fmt_time(s.starts_at)} · {s.subject}", 55))
                              for s in remaining[-5:]])))
    return True


async def begin_lesson_log(session: AsyncSession, app, class_session: ClassSession) -> Out:  # noqa: ANN001
    """User picked a class from '📝 Yozilmagan darslar'."""
    if class_session.status != "pending":
        return Out("Bu dars allaqachon qayd etilgan yoki o'tkazib yuborilgan.")
    await st.set_state(session, app.chat_id, st.AWAIT_LOG, {"session_id": class_session.id},
                       ttl_minutes=app.settings.lesson_log_timeout_minutes)
    return Out(
        f"📝 <b>{esc(class_session.subject)}</b> ({timeutil.fmt_date(class_session.class_date)})\n"
        "Qaysi mavzu o'tildi va uy vazifasi berildimi? Matn yoki ovozli xabar yuboring."
    )


# ============================================================================ study mode
async def enter_study(session: AsyncSession, app, course_id: int) -> Out:  # noqa: ANN001
    course = await session.get(Course, course_id) if course_id else None
    await _close_study(session, app)
    study = StudySession(course_id=course.id if course else None, topic=course.title if course else None,
                         started_at=timeutil.now())
    session.add(study)
    await session.flush()
    await st.set_state(session, app.chat_id, st.STUDY, {"course_id": course.id if course else None,
                                                         "study_session_id": study.id})
    return Out(await reports.course_brief(session, course), ui.study_kb())


async def _close_study(session: AsyncSession, app) -> StudySession | None:  # noqa: ANN001
    state, ctx = await st.get_state(session, app.chat_id)
    if state != st.STUDY:  # study mode may be sitting underneath an interrupting state
        previous = ctx.get("_prev") or {}
        if previous.get("state") != st.STUDY:
            return None
        ctx = previous.get("context") or {}
    study_ctx = ctx
    study = await session.get(StudySession, study_ctx.get("study_session_id") or 0)
    if study is not None and study.ended_at is None:
        study.ended_at = timeutil.now()
    await st.reset_state(session, app.chat_id)
    return study


async def exit_study(session: AsyncSession, app) -> Out:  # noqa: ANN001
    study = await _close_study(session, app)
    if study is None:
        return Out("O'qish rejimi yoqilmagan.", ui.main_menu())
    minutes = int((study.ended_at - study.started_at).total_seconds() // 60)
    return Out(
        f"🏁 <b>Mashg'ulot yakunlandi</b>\n⏱ Davomiyligi: {timeutil.fmt_duration(max(minutes, 1))}\n"
        f"💬 Savol-javoblar: {study.turns} ta\n\nBarakalla! Natijalar haftalik hisobotga qo'shildi.",
        ui.main_menu(),
    )


async def _study_context(session: AsyncSession, ctx: dict) -> tuple[Course | None, str]:
    course = await session.get(Course, ctx["course_id"]) if ctx.get("course_id") else None
    if course is None:
        return None, "Study mode is active with a free topic (no specific course selected)."
    logs = await lessons.recent_logs(session, course.id, limit=4)
    lines = [f"Study mode is active. Selected course: {course.title} (group {course.group_key})."]
    if logs:
        lines.append("Most recent lessons of this course (from the student's own logs):")
        lines += [f"- {entry.class_date.isoformat()}: {entry.topic}" + (
            f" | homework: {entry.homework}" if entry.homework else "") for entry in logs]
    else:
        lines.append("No lesson logs exist for this course yet.")
    return course, "\n".join(lines)


async def study_turn(session: AsyncSession, app, text: str, outbox: list[Out]) -> None:  # noqa: ANN001
    state, ctx = await st.get_state(session, app.chat_id)
    _, extra = await _study_context(session, ctx)
    tool_ctx = ToolContext(session=session, app=app, state=state, state_ctx=ctx)
    reply = await run_agent(STUDY_AGENT, tool_ctx, text, extra_context=extra)
    study = await session.get(StudySession, ctx.get("study_session_id") or 0)
    if study is not None:
        study.turns += 1
    if reply.text:
        outbox.append(Out(reply.text, ui.study_kb(), markdown=True))
    outbox.extend(Out(text_html, keyboard) for text_html, keyboard in reply.cards)


async def start_quiz(session: AsyncSession, app, course_id: int | None, outbox: list[Out],  # noqa: ANN001
                     review_first: bool = False) -> None:
    """Send a batch of quiz polls: due cards first (spaced repetition), topped up with new ones."""
    course = await session.get(Course, course_id) if course_id else None
    items: list[QuizItem] = await quiz.due_items(session, course.id if course else None, limit=5) if (
        review_first or course) else []
    if len(items) < 3:
        logs = await lessons.recent_logs(session, course.id if course else None, limit=5)
        if course is None and not logs and not items:
            outbox.append(Out("🧠 Test tuzish uchun avval biror fan tanlang yoki dars qaydini kiriting.",
                              ui.study_courses_kb([(c.id, c.title) for c in await _courses(session)])))
            return
        target = course or (logs[0].course if logs else None)
        fresh = await quiz.generate_items(session, app, course=target, topic=None,
                                          logs=[entry for entry in logs if target and entry.course_id == target.id],
                                          count=5 - len(items))
        items.extend(fresh)
    if not items:
        outbox.append(Out("😕 Savollarni tuzib bo'lmadi. Birozdan keyin qayta urinib ko'ring."))
        return
    await app.notifier.send(f"🧠 <b>Test: {len(items)} ta savol</b>\nHar biriga javob bering — natijani oxirida aytaman.")
    await quiz.send_batch(session, app, items)


async def _courses(session: AsyncSession) -> list[Course]:
    return await timetable.list_courses(session)


async def next_flashcard(session: AsyncSession, app, outbox: list[Out]) -> None:  # noqa: ANN001
    state, ctx = await st.get_state(session, app.chat_id)
    course_id = ctx.get("course_id") if state == st.STUDY else None
    items = await quiz.due_items(session, course_id, limit=1)
    if not items and course_id:
        course = await session.get(Course, course_id)
        logs = await lessons.recent_logs(session, course_id, limit=5)
        items = (await quiz.generate_items(session, app, course=course, topic=None, logs=logs, count=5))[:1]
    if not items:
        outbox.append(Out("🃏 Hozircha takrorlanadigan kartochka yo'q. 🎉\n"
                          "<i>Yangi savollar uchun «📖 O'qish rejimi»da fan tanlab, «🧠 Test»ni bosing.</i>"))
        return
    item = items[0]
    outbox.append(Out(f"🃏 <b>{esc(item.topic)}</b>\n\n{esc(item.question)}", ui.flashcard_front_kb(item.id)))


def flashcard_back(item: QuizItem) -> tuple[str, InlineKeyboardMarkup]:
    answer = str(item.options[item.correct_index])
    text = f"🃏 <b>{esc(item.topic)}</b>\n\n{esc(item.question)}\n\n✅ <b>Javob:</b> {esc(answer)}"
    if item.explanation:
        text += f"\n<i>{esc(item.explanation)}</i>"
    return text, ui.flashcard_back_kb(item.id)


def quiz_summary(summary: dict) -> Out:
    total, correct = summary["total"], summary["correct"]
    ratio = correct / total if total else 0
    verdict = "A'lo! 🎉" if ratio >= 0.8 else "Yaxshi, lekin takrorlash kerak. 💪" if ratio >= 0.5 else "Bu mavzuni qayta ko'rib chiqaylik. 📚"
    lines = [f"🧠 <b>Test natijasi: {correct} / {total}</b>", verdict]
    if summary["wrong_topics"]:
        lines += ["", "🎯 <b>Takrorlash kerak:</b>"] + [f"• {esc(t)}" for t in summary["wrong_topics"][:5]]
        lines.append("<i>Xato javob berilgan savollar 10 daqiqadan keyin kartochkalarda qayta chiqadi.</i>")
    return Out("\n".join(lines), ui.quiz_summary_kb(summary.get("course_id")))


# ============================================================================ free text / voice entry point
async def handle_text(app, text: str, *, input_kind: str = "text", reply_to_message_id: int | None = None) -> None:  # noqa: ANN001
    """Route one free-form user message (typed or transcribed) through the state machine."""
    text = text.strip()
    if not text:
        return
    outbox: list[Out] = []
    async with turn_lock, typing(app):
        try:
            async with session_scope() as session:
                await _dispatch(session, app, text, input_kind, reply_to_message_id, outbox)
        except LLMError as exc:
            log.error("all llm routes failed", extra={"error": str(exc)[:500]})
            await app.notifier.alert_admin("LLM ishlamayapti", str(exc), window_minutes=30)
            outbox = [Out(LLM_DOWN)]
    await deliver(app, outbox)


async def _dispatch(session: AsyncSession, app, text: str, input_kind: str, reply_to: int | None,  # noqa: ANN001
                    outbox: list[Out]) -> None:
    state, ctx = await st.get_state(session, app.chat_id)

    # 1) A Telegram reply to a specific "class ended" prompt always logs that class.
    if reply_to is not None:
        target = await lessons.session_by_prompt(session, reply_to)
        if target is not None and target.status == "pending":
            if await log_lesson(session, app, target, text, input_kind, outbox):
                return

    # 2) Waiting for a new time for a pending action.
    if state == st.AWAIT_TIME:
        await _handle_time_edit(session, app, text, ctx, outbox)
        return

    # 3) A confirmation card is open: typed yes/no resolves it, anything else is a new request.
    if state == st.CONFIRMING:
        word = _norm(text)
        action = await actions.get_action(session, ctx.get("action_id", ""))
        if action is not None and word in _YES | _NO:
            outbox.append(await resolve_action(session, app, action, word in _YES))
            return
        state, ctx = await st.pop_state(session, app.chat_id)

    # 4) A class just ended: the message is most likely the lesson report.
    if state == st.AWAIT_LOG:
        target = await lessons.get_session(session, ctx.get("session_id") or 0)
        if target is not None and target.status == "pending":
            if await log_lesson(session, app, target, text, input_kind, outbox):
                return
        else:
            state, ctx = await st.pop_state(session, app.chat_id)

    # 5) Study mode talks to the coach directly; otherwise the router picks an agent.
    if state == st.STUDY:
        await study_turn(session, app, text, outbox)
        return
    agent_name = await router.route(app, session, text)
    tool_ctx = ToolContext(session=session, app=app, state=state, state_ctx=ctx)
    reply = await run_agent(AGENTS[agent_name], tool_ctx, text)
    log.info("agent turn", extra={"agent": agent_name, "route": reply.route, "cards": len(reply.cards)})

    # Receipts/confirmation cards are authoritative; model prose is added only when it carries
    # information the cards do not (i.e. the agent also read data to answer a question).
    if reply.text and (not reply.cards or tool_ctx.used_read_tool):
        outbox.append(Out(reply.text, markdown=True))
    outbox.extend(Out(text_html, keyboard) for text_html, keyboard in reply.cards)
    if not outbox:
        outbox.append(Out("✅ Tayyor."))
    pending = await actions.latest_pending(session)
    if pending is not None and any(pending.id in (kb.inline_keyboard[0][0].callback_data or "")
                                   for _, kb in reply.cards if kb is not None):
        await st.set_state(session, app.chat_id, st.CONFIRMING, {"action_id": pending.id},
                           ttl_minutes=app.settings.action_ttl_minutes)
