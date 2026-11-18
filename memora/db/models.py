"""Persistent schema (PostgreSQL). All timestamps are timezone-aware."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, Time,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


# --------------------------------------------------------------------------- academics
class Course(Base):
    """A subject within a cohort; Lecture/Practice variants share one course."""

    __tablename__ = "courses"
    __table_args__ = (UniqueConstraint("group_key", "key", name="uq_course_group_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_key: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class TimetableEntry(Base):
    """One weekly recurring class slot for one of the user's cohorts."""

    __tablename__ = "timetable_entries"
    __table_args__ = (
        UniqueConstraint("group_key", "weekday", "start_time", name="uq_entry_slot"),
        Index("ix_entry_active_weekday", "is_active", "weekday"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_key: Mapped[str] = mapped_column(String(64))
    group_raw: Mapped[str] = mapped_column(String(100))
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday
    slot_no: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    subject: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str | None] = mapped_column(String(20))  # lecture | practice | None
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    room: Mapped[str | None] = mapped_column(String(100))
    teacher: Mapped[str | None] = mapped_column(String(200))
    shared_groups: Mapped[str | None] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())

    course: Mapped[Course] = relationship(lazy="joined")


class TimetableChange(Base):
    """Audit trail of every detected timetable diff."""

    __tablename__ = "timetable_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    detected_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    change_type: Mapped[str] = mapped_column(String(16))  # added | removed | changed
    group_key: Mapped[str] = mapped_column(String(64))
    weekday: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[time] = mapped_column(Time)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ClassSession(Base):
    """A concrete occurrence of a class on a date; drives the reflection pipeline."""

    __tablename__ = "class_sessions"
    __table_args__ = (UniqueConstraint("entry_id", "class_date", name="uq_session_entry_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int | None] = mapped_column(ForeignKey("timetable_entries.id", ondelete="SET NULL"))
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    class_date: Mapped[date] = mapped_column(Date, index=True)
    starts_at: Mapped[datetime] = mapped_column(TS)
    ends_at: Mapped[datetime] = mapped_column(TS)
    subject: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|logged|skipped|cancelled
    prompt_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())

    course: Mapped[Course] = relationship(lazy="joined")


class LessonLog(Base):
    """What was covered in a class and what homework was assigned (Uzbek)."""

    __tablename__ = "lesson_logs"
    __table_args__ = (Index("ix_lesson_course_date", "course_id", "class_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("class_sessions.id", ondelete="SET NULL"), unique=True)
    class_date: Mapped[date] = mapped_column(Date)
    topic: Mapped[str] = mapped_column(Text)
    homework: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str] = mapped_column(Text)
    input_kind: Mapped[str] = mapped_column(String(8), default="text")  # text | voice
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())

    course: Mapped[Course] = relationship(lazy="joined")


# --------------------------------------------------------------------------- productivity
class Task(Base):
    """Local mirror of a Google Task plus the precise reminder timestamp Google cannot store."""

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_task_status_remind", "status", "remind_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    google_task_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(TS)
    has_time: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    remind_at: Mapped[datetime | None] = mapped_column(TS)
    reminded_at: Mapped[datetime | None] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|completed|deleted
    completed_at: Mapped[datetime | None] = mapped_column(TS)
    source: Mapped[str] = mapped_column(String(16), default="bot")  # bot|homework|google
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id", ondelete="SET NULL"))
    needs_push: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    google_updated: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())


class CalendarEvent(Base):
    """Read cache of Google Calendar so menu buttons never wait on the network."""

    __tablename__ = "calendar_events"
    __table_args__ = (Index("ix_calevent_start", "starts_at"),)

    google_event_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    summary: Mapped[str] = mapped_column(String(500), default="")
    location: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(TS)
    ends_at: Mapped[datetime] = mapped_column(TS)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    html_link: Mapped[str | None] = mapped_column(String(500))
    synced_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class PendingAction(Base):
    """A mutation awaiting human confirmation through inline buttons."""

    __tablename__ = "pending_actions"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|confirmed|cancelled|expired
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(TS)
    resolved_at: Mapped[datetime | None] = mapped_column(TS)


# --------------------------------------------------------------------------- conversation
class ConversationState(Base):
    """Finite-state machine position for the chat, persisted across restarts."""

    __tablename__ = "conversation_state"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    state: Mapped[str] = mapped_column(String(32), default="idle")
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())


class MemoryMessage(Base):
    """Episodic memory: recent dialogue turns, partitioned per agent domain."""

    __tablename__ = "memory_messages"
    __table_args__ = (Index("ix_memory_domain_time", "domain", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Notification(Base):
    """Dispatch ledger. The unique dedupe_key is what makes every alert exactly-once-or-less."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="claimed")  # claimed|sent|failed
    attempts: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(TS)


# --------------------------------------------------------------------------- study
class StudySession(Base):
    __tablename__ = "study_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id", ondelete="SET NULL"))
    topic: Mapped[str | None] = mapped_column(String(300))
    started_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(TS)
    turns: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class QuizItem(Base):
    """An active-recall card with SM-2 style scheduling state."""

    __tablename__ = "quiz_items"
    __table_args__ = (Index("ix_quiz_due", "due_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id", ondelete="SET NULL"))
    lesson_log_id: Mapped[int | None] = mapped_column(ForeignKey("lesson_logs.id", ondelete="SET NULL"))
    topic: Mapped[str] = mapped_column(String(200))
    question: Mapped[str] = mapped_column(String(300))
    options: Mapped[list[Any]] = mapped_column(JSONB)
    correct_index: Mapped[int] = mapped_column(Integer)
    explanation: Mapped[str | None] = mapped_column(String(200))
    ease: Mapped[float] = mapped_column(Float, default=2.5, server_default=text("2.5"))
    interval_days: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    repetitions: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    lapses: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    due_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())

    course: Mapped[Course | None] = relationship(lazy="joined")


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"
    __table_args__ = (Index("ix_attempt_time", "answered_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("quiz_items.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(12))  # poll | flashcard
    correct: Mapped[bool] = mapped_column(Boolean)
    answered_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class QuizPoll(Base):
    """Maps a Telegram poll id back to its card so poll_answer updates can be scored."""

    __tablename__ = "quiz_polls"

    poll_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("quiz_items.id", ondelete="CASCADE"))
    batch_id: Mapped[str] = mapped_column(String(16), index=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    answered: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    correct: Mapped[bool | None] = mapped_column(Boolean)
    sent_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


# --------------------------------------------------------------------------- misc
class KV(Base):
    """Small durable key/value store (sync cursors, heartbeats, mute windows)."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())
