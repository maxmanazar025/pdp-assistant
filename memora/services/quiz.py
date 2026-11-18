"""Active-recall engine: quiz generation, Telegram quiz polls, flashcards, spaced repetition."""
from __future__ import annotations

import logging
import random
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from memora import timeutil
from memora.db.models import Course, LessonLog, QuizAttempt, QuizItem, QuizPoll
from memora.textfmt import truncate

log = logging.getLogger(__name__)

# Telegram Bot API limits for sendPoll.
MAX_QUESTION, MAX_OPTION, MAX_EXPLANATION = 300, 100, 200
RELEARN_DELAY = timedelta(minutes=10)

_GEN_SYSTEM = """Sen universitet talabasi uchun test savollari tuzuvchi o'qituvchisan.
Berilgan mavzu va dars qaydlari asosida bir to'g'ri javobli test savollari tuz.

Qoidalar:
- Hamma matn o'zbek tilida (lotin yozuvida). Atamalar va kod inglizcha qolishi mumkin.
- Savollar tushunishni tekshirsin, shunchaki yodlashni emas. Har savol alohida g'oyani tekshirsin.
- Har savolda aynan 4 ta variant, faqat bittasi to'g'ri. Noto'g'ri variantlar ishonarli bo'lsin.
- Cheklovlar: savol <= 250 belgi, har bir variant <= 90 belgi, izoh <= 180 belgi.
- Markdown yoki HTML ishlatma, oddiy matn yoz.
- "topic" — savol tegishli bo'lgan qisqa mavzu nomi (2-5 so'z).

Faqat JSON qaytar:
{"questions":[{"topic":"...","question":"...","options":["...","...","...","..."],"correct_index":0,"explanation":"..."}]}"""


@dataclass(frozen=True, slots=True)
class WeakTopic:
    course: str | None
    topic: str
    attempts: int
    accuracy: float


def sm2_update(item: QuizItem, correct: bool, now: datetime | None = None) -> None:
    """SM-2 style scheduling. Wrong answers come back within the same sitting."""
    now = now or timeutil.now()
    if correct:
        item.repetitions += 1
        if item.repetitions == 1:
            item.interval_days = 1
        elif item.repetitions == 2:
            item.interval_days = 3
        else:
            item.interval_days = max(item.interval_days + 1, round(item.interval_days * item.ease))
        item.ease = min(2.8, round(item.ease + 0.1, 2))
        item.due_at = now + timedelta(days=item.interval_days)
    else:
        item.repetitions = 0
        item.interval_days = 0
        item.lapses += 1
        item.ease = max(1.3, round(item.ease - 0.2, 2))
        item.due_at = now + RELEARN_DELAY


def _valid_question(raw: dict) -> dict | None:
    try:
        question = str(raw["question"]).strip()
        options = [str(o).strip() for o in raw["options"]]
        correct = int(raw["correct_index"])
    except (KeyError, TypeError, ValueError):
        return None
    if not question or not 2 <= len(options) <= 10 or not 0 <= correct < len(options):
        return None
    if any(not o for o in options) or len({o.casefold() for o in options}) != len(options):
        return None
    # Shuffle so the correct answer is not always in the position the model prefers.
    order = list(range(len(options)))
    random.shuffle(order)
    return {
        "topic": truncate(str(raw.get("topic") or "Umumiy"), 200),
        "question": truncate(question, MAX_QUESTION),
        "options": [truncate(options[i], MAX_OPTION) for i in order],
        "correct_index": order.index(correct),
        "explanation": truncate(str(raw.get("explanation") or ""), MAX_EXPLANATION) or None,
    }


async def generate_items(session: AsyncSession, app, *, course: Course | None, topic: str | None,  # noqa: ANN001
                         logs: list[LessonLog], count: int = 5) -> list[QuizItem]:
    """Ask the LLM for fresh questions grounded in the student's own lesson logs."""
    count = max(1, min(count, 10))
    context = []
    if course is not None:
        context.append(f"Fan: {course.title}")
    if topic:
        context.append(f"So'ralgan mavzu: {topic}")
    for entry in logs[:6]:
        context.append(f"- {entry.class_date.isoformat()} darsi: {entry.topic}" + (
            f" | uy vazifasi: {entry.homework}" if entry.homework else ""))
    weak = await weak_topics(session, course_id=course.id if course else None, limit=3)
    if weak:
        context.append("Talaba qiynalgan mavzular (shularga ko'proq e'tibor ber): " + ", ".join(w.topic for w in weak))
    existing = (
        await session.execute(
            select(QuizItem.question).where(QuizItem.course_id == (course.id if course else None))
            .order_by(QuizItem.id.desc()).limit(15)
        )
    ).scalars().all()
    if existing:
        context.append("Bu savollar allaqachon bor, takrorlama:\n" + "\n".join(f"* {q}" for q in existing))
    data = await app.llm.json(
        [
            {"role": "system", "content": _GEN_SYSTEM},
            {"role": "user", "content": "\n".join(context) + f"\n\n{count} ta savol tuz."},
        ],
        tier="smart", temperature=0.6,
    )
    raw_items = data.get("questions", []) if isinstance(data, dict) else data
    items: list[QuizItem] = []
    for raw in raw_items if isinstance(raw_items, list) else []:
        valid = _valid_question(raw) if isinstance(raw, dict) else None
        if valid is None:
            continue
        item = QuizItem(
            course_id=course.id if course else None, lesson_log_id=logs[0].id if logs else None,
            due_at=timeutil.now(), **valid,
        )
        session.add(item)
        items.append(item)
        if len(items) >= count:
            break
    await session.flush()
    return items


async def due_items(session: AsyncSession, course_id: int | None = None, limit: int = 5) -> list[QuizItem]:
    stmt = select(QuizItem).where(QuizItem.due_at <= timeutil.now()).order_by(QuizItem.due_at).limit(limit)
    if course_id:
        stmt = stmt.where(QuizItem.course_id == course_id)
    return list((await session.execute(stmt)).scalars())


async def due_count(session: AsyncSession) -> int:
    return (
        await session.execute(select(func.count(QuizItem.id)).where(QuizItem.due_at <= timeutil.now()))
    ).scalar_one()


async def record_answer(session: AsyncSession, item: QuizItem, correct: bool, mode: str) -> None:
    session.add(QuizAttempt(item_id=item.id, mode=mode, correct=correct, answered_at=timeutil.now()))
    sm2_update(item, correct)
    await session.flush()


async def weak_topics(session: AsyncSession, course_id: int | None = None, limit: int = 5) -> list[WeakTopic]:
    """Topics with at least two attempts and accuracy below 70%, worst first."""
    correct_sum = func.sum(case((QuizAttempt.correct.is_(True), 1), else_=0))
    stmt = (
        select(Course.title, QuizItem.topic, func.count(QuizAttempt.id), correct_sum)
        .join(QuizItem, QuizItem.id == QuizAttempt.item_id)
        .outerjoin(Course, Course.id == QuizItem.course_id)
        .group_by(Course.title, QuizItem.topic)
        .having(func.count(QuizAttempt.id) >= 2)
    )
    if course_id:
        stmt = stmt.where(QuizItem.course_id == course_id)
    rows = (await session.execute(stmt)).all()
    weak = [
        WeakTopic(course, topic, attempts, (correct or 0) / attempts)
        for course, topic, attempts, correct in rows
        if (correct or 0) / attempts < 0.7
    ]
    weak.sort(key=lambda w: (w.accuracy, -w.attempts))
    return weak[:limit]


async def send_batch(session: AsyncSession, app, items: list[QuizItem]) -> str:  # noqa: ANN001
    """Send items as native Telegram quiz polls and remember poll -> item mapping."""
    batch_id = secrets.token_hex(5)
    for item in items:
        message = await app.bot.send_poll(
            chat_id=app.chat_id,
            question=truncate(item.question, MAX_QUESTION),
            options=[truncate(str(o), MAX_OPTION) for o in item.options],
            type="quiz",
            correct_option_ids=[item.correct_index],
            explanation=item.explanation or None,
            is_anonymous=False,        # required to receive poll_answer updates
            allows_revoting=False,     # an answer is final, so the first attempt is what gets scored
            # Model-written text is sent as plain text: no parse mode, nothing to break.
            question_parse_mode=None,
            explanation_parse_mode=None,
        )
        session.add(QuizPoll(poll_id=message.poll.id, item_id=item.id, batch_id=batch_id,
                             message_id=message.message_id, sent_at=timeutil.now()))
    await session.flush()
    return batch_id


async def handle_poll_answer(session: AsyncSession, poll_id: str, option_ids: list[int]) -> dict | None:
    """Score one answer. Returns a batch summary dict once every poll of the batch is answered."""
    poll = await session.get(QuizPoll, poll_id)
    if poll is None or poll.answered or not option_ids:
        return None
    item = await session.get(QuizItem, poll.item_id)
    if item is None:
        return None
    correct = option_ids[0] == item.correct_index
    poll.answered, poll.correct = True, correct
    await record_answer(session, item, correct, "poll")
    batch = (await session.execute(select(QuizPoll).where(QuizPoll.batch_id == poll.batch_id))).scalars().all()
    if not all(p.answered for p in batch):
        return None
    wrong_items = (
        await session.execute(
            select(QuizItem).where(QuizItem.id.in_([p.item_id for p in batch if not p.correct]))
        )
    ).scalars().all() if any(not p.correct for p in batch) else []
    return {
        "total": len(batch),
        "correct": sum(1 for p in batch if p.correct),
        "wrong_topics": list(dict.fromkeys(i.topic for i in wrong_items)),
        "course_id": item.course_id,
    }


async def stats_between(session: AsyncSession, start: datetime, end: datetime) -> tuple[int, int]:
    """(attempts, correct) within a time window."""
    row = (
        await session.execute(
            select(func.count(QuizAttempt.id), func.sum(case((QuizAttempt.correct.is_(True), 1), else_=0)))
            .where(QuizAttempt.answered_at >= start, QuizAttempt.answered_at < end)
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)
