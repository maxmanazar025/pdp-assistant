"""Telegram UI vocabulary shared by the bot and the worker: menus, buttons, callback protocol.

Callback protocol
-----------------
``callback_data`` is ``<namespace>:<action>[:<arg>...]`` (ASCII, far below Telegram's
64-byte limit). Arguments are always database ids, never free text, so a button stays
valid across restarts and can be handled without any LLM call.

====  =========================================================================
ns    actions
====  =========================================================================
m     today | tomorrow | tasks | study | week | pending | review     (menu)
t     done:<task> | snz:<task>:<minutes> | del:<task> | undo:<task>  (tasks)
a     ok:<action> | edit:<action> | no:<action>                      (HITL)
c     prep:<entry> | quiz:<course>                                   (classes)
l     log:<session> | later:<session> | none:<session>               (reflection)
s     course:<course|0> | plan | quiz | cards | exit                 (study mode)
f     show:<item> | rate:<item>:<0|1> | stop                         (flashcards)
====  =========================================================================
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

# ---- persistent reply keyboard (deterministic fast path) -------------------------------
BTN_TODAY = "📅 Bugungi jadval"
BTN_TOMORROW = "📆 Ertangi jadval"
BTN_TASKS = "📋 Vazifalarim"
BTN_STUDY = "📖 O'qish rejimi"
BTN_WEEK = "📊 Haftalik hisobot"
MENU_BUTTONS = {BTN_TODAY: "today", BTN_TOMORROW: "tomorrow", BTN_TASKS: "tasks", BTN_STUDY: "study", BTN_WEEK: "week"}


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
            [KeyboardButton(text=BTN_TASKS), KeyboardButton(text=BTN_STUDY)],
            [KeyboardButton(text=BTN_WEEK)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Yozing yoki ovozli xabar yuboring…",
    )


def cb(*parts: object) -> str:
    data = ":".join(str(p) for p in parts)
    assert len(data.encode()) <= 64, data
    return data


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows]
    )


# ---- briefing / schedule -----------------------------------------------------------------
def briefing_kb(has_pending_logs: bool, has_due_cards: bool) -> InlineKeyboardMarkup:
    rows = [[("📋 Vazifalarim", cb("m", "tasks")), ("📆 Ertangi jadval", cb("m", "tomorrow"))]]
    extra = []
    if has_pending_logs:
        extra.append(("📝 Yozilmagan darslar", cb("m", "pending")))
    if has_due_cards:
        extra.append(("🧠 Takrorlash", cb("m", "review")))
    if extra:
        rows.append(extra)
    rows.append([("📖 O'qish rejimi", cb("m", "study"))])
    return _kb(rows)


def schedule_kb(is_today: bool) -> InlineKeyboardMarkup:
    other = ("📆 Ertangi jadval", cb("m", "tomorrow")) if is_today else ("📅 Bugungi jadval", cb("m", "today"))
    return _kb([[other, ("📋 Vazifalarim", cb("m", "tasks"))]])


def class_alert_kb(entry_id: int) -> InlineKeyboardMarkup:
    return _kb([[("📝 Darsga tayyorgarlik", cb("c", "prep", entry_id))]])


def prep_kb(course_id: int, has_material: bool) -> InlineKeyboardMarkup | None:
    return _kb([[("🧠 Tezkor test", cb("c", "quiz", course_id))]]) if has_material else None


# ---- tasks ----------------------------------------------------------------------------------
def reminder_kb(task_id: int) -> InlineKeyboardMarkup:
    return _kb([
        [("✅ Bajarildi", cb("t", "done", task_id))],
        [("⏰ +15 daqiqa", cb("t", "snz", task_id, 15)), ("⏰ +1 soat", cb("t", "snz", task_id, 60))],
    ])


def task_list_kb(task_ids: list[int]) -> InlineKeyboardMarkup | None:
    if not task_ids:
        return None
    buttons = [(f"✅ {i}", cb("t", "done", tid)) for i, tid in enumerate(task_ids, 1)]
    return _kb([buttons[i:i + 5] for i in range(0, len(buttons), 5)])


def task_created_kb(task_id: int) -> InlineKeyboardMarkup:
    return _kb([[("✅ Bajarildi", cb("t", "done", task_id)), ("🗑 O'chirish", cb("t", "del", task_id))]])


def task_done_kb(task_id: int) -> InlineKeyboardMarkup:
    return _kb([[("↩️ Qaytarish", cb("t", "undo", task_id))]])


# ---- human-in-the-loop ------------------------------------------------------------------------
def action_kb(action_id: str, can_edit_time: bool) -> InlineKeyboardMarkup:
    row = [("✅ Tasdiqlash", cb("a", "ok", action_id))]
    if can_edit_time:
        row.append(("✏️ Vaqtni o'zgartirish", cb("a", "edit", action_id)))
    return _kb([row, [("❌ Bekor qilish", cb("a", "no", action_id))]])


# ---- lesson reflection -------------------------------------------------------------------------
def lesson_prompt_kb(session_id: int) -> InlineKeyboardMarkup:
    return _kb([[("⏭ Keyinroq", cb("l", "later", session_id)), ("🚫 Dars bo'lmadi", cb("l", "none", session_id))]])


def pending_lessons_kb(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    return _kb([[(f"📝 {label}", cb("l", "log", sid))] for sid, label in items])


# ---- study mode -----------------------------------------------------------------------------------
def study_courses_kb(courses: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [[(f"📘 {title}", cb("s", "course", cid))] for cid, title in courses]
    rows.append([("✨ Erkin mavzu", cb("s", "course", 0))])
    return _kb(rows)


def study_kb() -> InlineKeyboardMarkup:
    return _kb([
        [("🗺 O'quv reja", cb("s", "plan")), ("🧠 Test", cb("s", "quiz"))],
        [("🃏 Kartochkalar", cb("s", "cards")), ("🏁 Tugatish", cb("s", "exit"))],
    ])


def quiz_summary_kb(course_id: int | None) -> InlineKeyboardMarkup:
    return _kb([[("🧠 Yana test", cb("c", "quiz", course_id or 0)), ("🃏 Kartochkalar", cb("s", "cards"))]])


def flashcard_front_kb(item_id: int) -> InlineKeyboardMarkup:
    return _kb([[("👁 Javobni ko'rish", cb("f", "show", item_id))], [("⏹ To'xtatish", cb("f", "stop"))]])


def flashcard_back_kb(item_id: int) -> InlineKeyboardMarkup:
    return _kb([[("✅ Bildim", cb("f", "rate", item_id, 1)), ("❌ Bilmadim", cb("f", "rate", item_id, 0))]])


def review_nudge_kb() -> InlineKeyboardMarkup:
    return _kb([[("🧠 Test bilan", cb("m", "review")), ("🃏 Kartochkalar", cb("s", "cards"))]])
