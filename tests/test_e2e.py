"""End-to-end simulation: real Postgres + real dispatcher/handlers/worker jobs,
with Telegram, the LLM and Google replaced by in-memory fakes and a controllable clock.

The simulated week starts on Tuesday 2026-11-17 and uses the real timetable rows.
"""
from __future__ import annotations

import time as _time

from sqlalchemy import func, select

from memora import timeutil
from memora.db import session_scope
from memora.db.models import (ClassSession, LessonLog, Notification, PendingAction, QuizAttempt, QuizItem, QuizPoll,
                              StudySession, Task, TimetableEntry)
from memora.services import state as st
from memora.services.notify import Notifier
from memora.timetable.sync import sync_timetable
from memora.worker import jobs

PY_PRACTICE = "Programming (Python) Level 4 (Practice)"


async def _state(env) -> tuple[str, dict]:
    async with session_scope() as session:
        return await st.get_state(session, env.app.chat_id)


async def _count(model, *where) -> int:
    async with session_scope() as session:
        return (await session.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


# ------------------------------------------------------------------------------ timetable
async def test_timetable_sync_diff_and_alert(env):
    report = await sync_timetable(env.app)
    assert report.first_sync and report.total_active == 14 and len(report.added) == 14
    assert "Dars jadvali yuklandi" in env.tg.last_text() and "14 ta dars" in env.tg.last_text()

    env.tg.clear()
    assert not (await sync_timetable(env.app)).has_changes
    assert env.tg.sent() == []                                   # unchanged sheet -> silence

    # The university moves Friday's lab and cancels Monday's 13:30 class.
    degree = env.csv["1454027058"].replace("220 Lab", "221 Lab")
    rows = degree.split("\r\n")
    block = next(i for i, row in enumerate(rows) if row.startswith("25-408 (UZ)"))
    for offset in range(4):
        cells = rows[block + offset].split(",")
        cells[4] = ""                                            # Monday slot 4
        rows[block + offset] = ",".join(cells)
    env.csv["1454027058"] = "\r\n".join(rows)
    env.clock.set("2026-11-19T18:00")                            # Thursday evening -> Friday is "ertaga"
    report = await sync_timetable(env.app)
    assert len(report.changed) == 1 and len(report.removed) == 1 and not report.added
    alert = env.tg.last_text()
    assert "Dars jadvali o'zgardi" in alert and "220 Lab → <b>221 Lab</b>" in alert
    assert "Olib tashlandi" in alert and "dushanba 13:30" in alert
    assert "ertangi darslarga ta'sir qiladi" in alert
    assert await _count(TimetableEntry, TimetableEntry.is_active.is_(True)) == 13


async def test_failed_or_missing_tab_never_wipes_schedule(env):
    from memora.integrations.sheets import SheetFetchError

    await sync_timetable(env.app)
    env.tg.clear()
    env.csv["1920812576"] = SheetFetchError("HTTP 500")          # English tab is down
    report = await sync_timetable(env.app)
    assert report.errors and not report.removed
    assert await _count(TimetableEntry, TimetableEntry.is_active.is_(True)) == 14
    assert "sinxronlashda xatolik" in env.tg.last_text()         # diagnostic alert to the owner

    env.csv["1454027058"] = env.csv["1454027058"].replace("25-408 (UZ)", "25-999 (UZ)")  # group renamed
    env.csv["1920812576"] = (await _fixture("sheet_english.csv"))
    report = await sync_timetable(env.app)
    assert report.missing_groups == ["25-408"] and not report.removed
    assert await _count(TimetableEntry, TimetableEntry.is_active.is_(True)) == 14


async def _fixture(name: str) -> str:
    from tests.conftest import FIXTURES

    return (FIXTURES / name).read_text(newline="")


# ------------------------------------------------------------------------------ proactive engine
async def test_briefing_alerts_and_restart_idempotency(env):
    await sync_timetable(env.app, announce=False)

    await jobs.tick(env.app, env.clock.set("2026-11-17T07:29"))
    assert env.tg.sent() == []
    await jobs.tick(env.app, env.clock.set("2026-11-17T07:30"))
    briefing = env.tg.last_text()
    assert "Xayrli tong" in briefing and "seshanba, 17-noyabr" in briefing
    assert briefing.count(PY_PRACTICE) == 2 and "Database Design" in briefing and "231 Room" in briefing

    # Same minute again, then a "container restart" (brand-new Notifier, same database).
    await jobs.tick(env.app, env.clock.current)
    env.app.notifier = Notifier(env.app.bot, env.app.chat_id)
    await jobs.tick(env.app, env.clock.set("2026-11-17T07:31"))
    assert len(env.tg.sent()) == 1

    # T-15 alert for the 09:30 class.
    env.tg.clear()
    await jobs.tick(env.app, env.clock.set("2026-11-17T09:14"))
    assert env.tg.sent() == []
    await jobs.tick(env.app, env.clock.set("2026-11-17T09:15"))
    alert = env.tg.last_text()
    assert "15 daqiqadan keyin dars" in alert and PY_PRACTICE in alert
    assert "231 Room" in alert and "Bekdurdiyev Boburjon" in alert and "25-408" in alert
    assert env.tg.find_callback("c:prep:")
    for minute in ("09:15", "09:16", "09:20"):
        await jobs.tick(env.app, env.clock.set(f"2026-11-17T{minute}"))
    assert len(env.tg.sent()) == 1

    # Worker was down across 10:45; it comes back at 10:50 and still alerts for 11:00.
    env.tg.clear()
    await jobs.tick(env.app, env.clock.set("2026-11-17T10:50"))
    assert "10 daqiqadan keyin dars" in env.tg.last_text() and "232 Room" in env.tg.last_text()


async def test_post_class_reflection_and_homework_pipeline(env):
    await sync_timetable(env.app, announce=False)
    env.tg.clear()

    # 10:50 ends the first half of a double Python period -> no prompt yet.
    await jobs.tick(env.app, env.clock.set("2026-11-17T10:50"))
    assert not [t for t in env.tg.texts() if "darsi tugadi" in t]
    assert (await _state(env))[0] == st.IDLE

    # 12:20, the exact minute the double period ends.
    env.tg.clear()
    await jobs.tick(env.app, env.clock.set("2026-11-17T12:20"))
    prompts = [t for t in env.tg.texts() if "darsi tugadi" in t]
    assert len(prompts) == 1 and PY_PRACTICE in prompts[0]
    state, ctx = await _state(env)
    assert state == st.AWAIT_LOG
    await jobs.tick(env.app, env.clock.set("2026-11-17T12:21"))
    assert len([t for t in env.tg.texts() if "darsi tugadi" in t]) == 1

    # The student answers in Uzbek.
    env.tg.clear()
    await env.say("Funksiyalar va rekursiya o'tildi, uyga 5 ta masala yechish")
    texts = env.tg.texts()
    assert any("Qayd etildi" in t and "Funksiyalar va rekursiya o'tildi" in t for t in texts)
    homework = next(t for t in texts if "Uy vazifasi Google Tasks'ga qo'shildi" in t)
    # Next Python session is Thursday 09:30 (lecture); reminder the evening before.
    assert "Keyingi dars: <b>indinga 09:30</b>" in homework and "ertaga 19:00" in homework
    assert (await _state(env))[0] == st.IDLE

    async with session_scope() as session:
        log = (await session.execute(select(LessonLog))).scalar_one()
        task = (await session.execute(select(Task))).scalar_one()
        cls = (await session.execute(select(ClassSession))).scalar_one()
    assert log.homework == "5 ta masala yechish" and log.task_id == task.id and cls.status == "logged"
    assert task.source == "homework" and task.google_task_id and not task.needs_push
    assert timeutil.iso(task.remind_at) == "2026-11-18T19:00:00+05:00"
    google_task = env.google.tasks[task.google_task_id]
    assert google_task["due"].startswith("2026-11-19") and "2026-11-18T19:00:00+05:00" in google_task["notes"]

    # "📝 Darsga tayyorgarlik" before Thursday's lecture pulls these notes back.
    await jobs.tick(env.app, env.clock.set("2026-11-19T09:15"))
    env.tg.clear()
    async with session_scope() as session:
        thursday = (await session.execute(select(TimetableEntry).where(
            TimetableEntry.weekday == 3, TimetableEntry.slot_no == 2, TimetableEntry.group_key == "25-408"))).scalar_one()
    await env.press(f"c:prep:{thursday.id}")
    prep = env.tg.last_text()
    assert "Funksiyalar va rekursiya o'tildi" in prep and "5 ta masala yechish" in prep and "Bajarilmagan vazifalar" in prep


async def test_offtopic_message_while_awaiting_log_is_routed_normally(env):
    await sync_timetable(env.app, announce=False)
    await jobs.tick(env.app, env.clock.set("2026-11-17T12:20"))
    env.llm.script = [("tool", "create_task", {"title": "Onamga qo'ng'iroq qilish", "due": "2026-11-17T18:00"}),
                      ("text", "Qo'shdim.")]
    await env.say("soat 6 da onamga qo'ng'iroq qilishni eslat")
    assert any("Vazifa qo'shildi" in t for t in env.tg.texts())
    assert await _count(LessonLog) == 0
    assert (await _state(env))[0] == st.AWAIT_LOG                # still waiting for the lesson report

    # "Dars bo'lmadi" closes the session without a log.
    await env.press(env.tg.find_callback("l:none:"))
    assert (await _state(env))[0] == st.IDLE
    assert await _count(ClassSession, ClassSession.status == "cancelled") == 1


# ------------------------------------------------------------------------------ reminders
async def test_task_reminder_snooze_complete(env):
    await sync_timetable(env.app, announce=False)
    env.clock.set("2026-11-17T14:00")
    env.llm.script = [("tool", "create_task", {"title": "Laboratoriya ishini topshirish", "due": "2026-11-17T18:00"})]
    await env.say("bugun 18:00 da laboratoriya ishini topshirishni eslat")
    async with session_scope() as session:
        task = (await session.execute(select(Task))).scalar_one()
    assert timeutil.iso(task.remind_at) == "2026-11-17T18:00:00+05:00" and task.has_time

    env.tg.clear()
    await jobs.tick(env.app, env.clock.set("2026-11-17T17:59"))
    assert env.tg.sent() == []
    await jobs.tick(env.app, env.clock.set("2026-11-17T18:00"))
    reminder = env.tg.sent()[-1]
    assert "Eslatma" in reminder.text and "Laboratoriya ishini topshirish" in reminder.text
    labels = [b.text for row in reminder.reply_markup.inline_keyboard for b in row]
    assert labels == ["✅ Bajarildi", "⏰ +15 daqiqa", "⏰ +1 soat"]
    await jobs.tick(env.app, env.clock.set("2026-11-17T18:01"))
    assert len(env.tg.sent()) == 1                               # no repeat

    await env.press(f"t:snz:{task.id}:15", message_text=reminder.text)
    await jobs.tick(env.app, env.clock.set("2026-11-17T18:10"))
    assert len(env.tg.sent()) == 1
    await jobs.tick(env.app, env.clock.set("2026-11-17T18:16"))
    assert len(env.tg.sent()) == 2                               # snoozed reminder fires once

    await env.press(f"t:done:{task.id}", message_text=reminder.text)
    async with session_scope() as session:
        task = await session.get(Task, task.id)
    assert task.status == "completed" and env.google.tasks[task.google_task_id]["status"] == "completed"
    await jobs.tick(env.app, env.clock.set("2026-11-17T19:30"))
    assert len(env.tg.sent()) == 2


async def test_google_outage_is_buffered_and_never_duplicated(env):
    env.clock.set("2026-11-17T14:00")
    env.google.fail = True
    env.llm.route_override = "tasks"
    env.llm.script = [("tool", "create_task", {"title": "Kitobni qaytarish", "due": "2026-11-20"})]
    await env.say("juma kuni kitobni qaytarish kerak")
    assert any("keyinroq avtomatik sinxronlanadi" in t for t in env.tg.texts())
    async with session_scope() as session:
        task = (await session.execute(select(Task))).scalar_one()
    assert task.needs_push and task.google_task_id is None and not task.has_time
    assert timeutil.iso(task.remind_at) == "2026-11-20T09:00:00+05:00"   # date-only -> 09:00 reminder

    await jobs.google_sync(env.app)                              # still down
    env.google.fail = False
    await jobs.google_sync(env.app)
    await jobs.google_sync(env.app)
    assert env.google.task_creates == 1 and len(env.google.tasks) == 1
    async with session_scope() as session:
        task = await session.get(Task, task.id)
    assert not task.needs_push and task.google_task_id

    # Completed in the Google Tasks app -> pulled back.
    env.google.tasks[task.google_task_id]["status"] = "completed"
    await jobs.google_sync(env.app)
    async with session_scope() as session:
        assert (await session.get(Task, task.id)).status == "completed"


# ------------------------------------------------------------------------------ HITL + agents
async def test_calendar_event_requires_confirmation_and_detects_class_conflict(env):
    await sync_timetable(env.app, announce=False)
    env.clock.set("2026-11-17T14:00")
    env.tg.clear()
    env.llm.script = [("tool", "propose_event", {"title": "Loyiha uchrashuvi", "start": "2026-11-19T09:30",
                                                  "duration_minutes": 60, "location": "Kutubxona"}),
                      ("text", "Tadbir yaratildi!")]             # model over-claims; must be suppressed
    await env.say("payshanba 9:30 da kutubxonada loyiha uchrashuvi qo'y")
    card = env.tg.sent()[-1]
    assert "Yangi tadbir qo'shilsinmi?" in card.text and "Kutubxona" in card.text
    assert "Vaqt to'qnashuvi" in card.text and "Programming (Python) Level 4 (Lecture)" in card.text
    assert not any("Tadbir yaratildi!" in t for t in env.tg.texts())
    assert [b.text for row in card.reply_markup.inline_keyboard for b in row] == [
        "✅ Tasdiqlash", "✏️ Vaqtni o'zgartirish", "❌ Bekor qilish"]
    assert env.google.events == {} and (await _state(env))[0] == st.CONFIRMING

    # ✏️ change the time with a deterministic Uzbek phrase, then confirm by typing "ha".
    await env.press(env.tg.find_callback("a:edit:"))
    assert (await _state(env))[0] == st.AWAIT_TIME
    json_calls_before = len(env.llm.json_calls)
    await env.say("juma kuni soat 4 da")
    assert len(env.llm.json_calls) == json_calls_before          # parsed without any LLM call
    card = env.tg.sent()[-1]
    assert "juma, 20-noyabr 16:00–17:00" in card.text and "Vaqt to'qnashuvi" not in card.text
    await env.say("ha")
    assert "Tadbir qo'shildi" in env.tg.last_text()
    (event,) = env.google.events.values()
    assert event["start"]["dateTime"] == "2026-11-20T16:00:00+05:00" and event["location"] == "Kutubxona"
    assert (await _state(env))[0] == st.IDLE

    # Pressing the stale button again must not create a second event.
    async with session_scope() as session:
        action = (await session.execute(select(PendingAction))).scalar_one()
    await env.press(f"a:ok:{action.id}")
    assert len(env.google.events) == 1 and "eskirgan" in env.tg.last_text()


async def test_cancel_and_task_delete_confirmation(env):
    env.clock.set("2026-11-17T14:00")
    env.llm.script = [("tool", "create_task", {"title": "Eski vazifa"})]
    await env.say("eski vazifa qo'sh")
    async with session_scope() as session:
        task = (await session.execute(select(Task))).scalar_one()
    await env.press(f"t:del:{task.id}")
    assert "Vazifa o'chirilsinmi?" in env.tg.last_text()
    await env.press(env.tg.find_callback("a:no:"))
    async with session_scope() as session:
        assert (await session.get(Task, task.id)).status == "pending"
    await env.press(f"t:del:{task.id}")
    await env.press(env.tg.find_callback("a:ok:"))
    async with session_scope() as session:
        assert (await session.get(Task, task.id)).status == "deleted"
    assert env.google.tasks == {}


# ------------------------------------------------------------------------------ security / fast path
async def test_unauthorized_users_are_ignored(env):
    await env.say("/start", user_id=999)
    await env.say("salom", user_id=999)
    await env.press("m:today", user_id=999)
    await env.answer_poll("poll1", 0, user_id=999)
    assert env.tg.calls == [] and env.llm.chat_calls == [] and env.llm.json_calls == []
    assert await _count(Notification) == 0


async def test_fast_path_buttons_use_no_llm_and_are_fast(env):
    await sync_timetable(env.app, announce=False)
    env.clock.set("2026-11-17T08:00")
    env.tg.clear()
    timings = []
    for label in ("📅 Bugungi jadval", "📆 Ertangi jadval", "📋 Vazifalarim", "📖 O'qish rejimi", "📊 Haftalik hisobot"):
        started = _time.perf_counter()
        await env.say(label)
        timings.append((_time.perf_counter() - started) * 1000)
    texts = env.tg.texts()
    assert "Bugun — seshanba, 17-noyabr" in texts[0] and PY_PRACTICE in texts[0]
    assert "Ertaga — chorshanba, 18-noyabr" in texts[1] and "Dars yo'q" in texts[1]
    assert "Vazifalarim" in texts[2] and "O'qish rejimi" in texts[3] and "Haftalik hisobot" in texts[4]
    assert env.llm.chat_calls == [] and env.llm.json_calls == []
    assert max(timings) < 300, timings


# ------------------------------------------------------------------------------ study coach
async def test_study_mode_quiz_and_spaced_repetition(env):
    await sync_timetable(env.app, announce=False)
    env.clock.set("2026-11-17T20:00")
    await env.say("📖 O'qish rejimi")
    course_cb = next(
        b.callback_data for row in env.tg.sent()[-1].reply_markup.inline_keyboard for b in row
        if "Programming (Python)" in b.text)
    await env.press(course_cb)
    assert (await _state(env))[0] == st.STUDY

    env.llm.script = [("tool", "get_lesson_logs", {"course": "python"}),
                      ("text", "## Reja\n1. **Rekursiya** — 20 daqiqa\n```python\ndef f(n):\n    return 1 if n < 2 else n * f(n - 1)\n```")]
    env.tg.clear()
    await env.say("rekursiyani tushuntir")
    answer = env.tg.sent()[-1].text
    assert '<pre><code class="language-python">' in answer and "n &lt; 2" in answer and "<b>Rekursiya</b>" in answer
    system_prompt = env.llm.chat_calls[0][0]["content"]
    assert "Selected course: Programming (Python) Level 4" in system_prompt

    await env.press("s:quiz")
    polls = env.tg.sent("SendPoll")
    assert len(polls) == 5 and all(p.type == "quiz" and p.is_anonymous is False for p in polls)
    assert all(len(p.correct_option_ids) == 1 and p.explanation_parse_mode is None for p in polls)
    async with session_scope() as session:
        mapping = {p.poll_id: await session.get(QuizItem, p.item_id)
                   for p in (await session.execute(select(QuizPoll))).scalars()}
    env.tg.clear()
    for index, (poll_id, item) in enumerate(sorted(mapping.items())):
        wrong = (item.correct_index + 1) % 4
        await env.answer_poll(poll_id, item.correct_index if index < 3 else wrong)
    summary = env.tg.last_text()
    assert "Test natijasi: 3 / 5" in summary and "Takrorlash kerak" in summary
    assert await _count(QuizAttempt) == 5

    async with session_scope() as session:
        items = (await session.execute(select(QuizItem).order_by(QuizItem.id))).scalars().all()
    right = [i for i in items if i.repetitions == 1]
    wrong = [i for i in items if i.lapses == 1]
    assert len(right) == 3 and len(wrong) == 2
    assert all(timeutil.iso(i.due_at) == "2026-11-18T20:00:00+05:00" for i in right)   # +1 day
    assert all(timeutil.iso(i.due_at) == "2026-11-17T20:10:00+05:00" for i in wrong)   # relearn in 10 min

    # Flashcards: the two missed cards come back after 10 minutes.
    env.clock.set("2026-11-17T20:15")
    env.tg.clear()
    await env.press("s:cards")
    assert env.tg.find_callback("f:show:")
    item_id = int(env.tg.find_callback("f:show:").split(":")[2])
    await env.press(f"f:show:{item_id}")
    assert "Javob:" in env.tg.last_text()
    await env.press(f"f:rate:{item_id}:1")
    async with session_scope() as session:
        assert (await session.get(QuizItem, item_id)).repetitions == 1

    await env.press("s:exit")
    assert "Mashg'ulot yakunlandi" in env.tg.last_text() and (await _state(env))[0] == st.IDLE
    async with session_scope() as session:
        study = (await session.execute(select(StudySession))).scalar_one()
    assert study.ended_at is not None and study.turns == 1


# ------------------------------------------------------------------------------ digest / resilience
async def test_weekly_digest_once_on_sunday(env):
    await sync_timetable(env.app, announce=False)
    await jobs.tick(env.app, env.clock.set("2026-11-17T12:20"))
    await env.say("Rekursiya o'tildi, vazifa yo'q")
    env.tg.clear()
    await jobs.tick(env.app, env.clock.set("2026-11-21T20:00"))    # Saturday: nothing
    assert not [t for t in env.tg.texts() if "Haftalik hisobot" in t]
    await jobs.tick(env.app, env.clock.set("2026-11-22T20:00"))    # Sunday evening
    await jobs.tick(env.app, env.clock.set("2026-11-22T20:05"))
    digests = [t for t in env.tg.texts() if "Haftalik hisobot" in t]
    assert len(digests) == 1
    assert "16-noyabr – 22-noyabr" in digests[0] and "Qayd etilgan: <b>1</b>" in digests[0]
    assert "Programming (Python) Level 4: Rekursiya o'tildi" in digests[0]


async def test_llm_outage_degrades_gracefully(env):
    await sync_timetable(env.app, announce=False)
    env.llm.fail = True
    env.tg.clear()
    await env.say("python dekoratorlarini tushuntir")
    texts = env.tg.texts()
    assert any("AI xizmati javob bermayapti" in t for t in texts)
    assert any("LLM ishlamayapti" in t for t in texts)            # diagnostic alert to the owner
    env.tg.clear()
    await env.say("📅 Bugungi jadval")                              # fast path unaffected
    assert "Bugun" in env.tg.last_text()

    # A lesson report during the outage is stored verbatim instead of being lost.
    await jobs.tick(env.app, env.clock.set("2026-11-17T12:20"))
    await env.say("OOP asoslari, vazifa berilmadi")
    async with session_scope() as session:
        log = (await session.execute(select(LessonLog))).scalar_one()
    assert log.topic == "OOP asoslari, vazifa berilmadi"
    assert any("o'z holicha saqlandi" in t for t in env.tg.texts())


async def test_pause_mutes_class_notifications_but_not_task_reminders(env):
    await sync_timetable(env.app, announce=False)
    env.clock.set("2026-11-17T07:00")
    env.llm.script = [("tool", "create_task", {"title": "Dori ichish", "due": "2026-11-17T09:20"})]
    await env.say("9:20 da dori ichishni eslat")
    await env.say("/pauza 2")
    env.tg.clear()
    for minute in ("07:30", "09:15", "09:20", "12:20"):
        await jobs.tick(env.app, env.clock.set(f"2026-11-17T{minute}"))
    texts = env.tg.texts()
    assert len(texts) == 1 and "Dori ichish" in texts[0]
    await env.say("/davom")
    await jobs.tick(env.app, env.clock.set("2026-11-17T13:15"))
    assert any("15 daqiqadan keyin dars" in t for t in env.tg.texts())


async def test_failed_delivery_is_retried_with_backoff_and_sent_once(env):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage
    from sqlalchemy import text

    attempts = {"n": 0}
    original = env.tg.make_request

    async def flaky(bot, method, timeout=None):  # noqa: ANN001
        if isinstance(method, SendMessage) and attempts["n"] == 0:
            attempts["n"] += 1
            raise TelegramNetworkError(method=method, message="connection reset")
        return await original(bot, method, timeout)

    env.tg.make_request = flaky

    async def send():
        return await env.app.notifier.send("salom")

    assert await env.app.notifier.dispatch_once("k1", "test", send) is False       # network error
    assert await env.app.notifier.dispatch_once("k1", "test", send) is False       # backoff not elapsed
    async with session_scope() as session:
        await session.execute(text("UPDATE notifications SET created_at = now() - interval '45 seconds'"))
    assert await env.app.notifier.dispatch_once("k1", "test", send) is True        # retried, delivered
    assert await env.app.notifier.dispatch_once("k1", "test", send) is False       # never again
    assert len(env.tg.sent()) == 1
    async with session_scope() as session:
        row = (await session.execute(select(Notification))).scalar_one()
    assert row.status == "sent" and row.attempts == 2
