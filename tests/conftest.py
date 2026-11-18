"""Test harness: real Postgres, fake Telegram / LLM / Google, controllable clock.

The database tests need a PostgreSQL instance; point TEST_DATABASE_URL at an empty
database (default: postgresql+asyncpg://memora:memora@127.0.0.1:5432/memora_test).
They are skipped automatically when it is unreachable.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://memora:memora@127.0.0.1:5432/memora_test")
os.environ["DATABASE_URL"] = TEST_DB
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST-TOKEN-not-real"
os.environ["GEMINI_API_KEY"] = "test-key"
os.environ["LOG_JSON"] = "false"
for _var in ("OPENAI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "GOOGLE_REFRESH_TOKEN"):
    os.environ.pop(_var, None)

from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.types import Chat, Message, Poll, PollOption  # noqa: E402

from memora import timeutil  # noqa: E402
from memora.agents.router import heuristic_route  # noqa: E402
from memora.app import build_app  # noqa: E402
from memora.config import get_settings  # noqa: E402
from memora.integrations.google import GoogleError  # noqa: E402
from memora.llm.client import LLMError, LLMResult  # noqa: E402

TZ = ZoneInfo("Asia/Tashkent")
OWNER = 812304675
_DISPATCHER = None
FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- fake Telegram
class FakeTelegram(BaseSession):
    """Records every Bot API call and answers with plausible objects."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[Any] = []
        self._message_id = 1000
        self._poll_id = 0

    async def close(self) -> None:
        return None

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover - not used
        yield b""

    def _message(self, chat_id: int, **extra: Any) -> Message:
        self._message_id += 1
        return Message(message_id=self._message_id, date=datetime.now(TZ), chat=Chat(id=chat_id, type="private"), **extra)

    async def make_request(self, bot, method, timeout=None):  # noqa: ANN001
        self.calls.append(method)
        name = type(method).__name__
        if name == "SendMessage":
            return self._message(method.chat_id, text=method.text)
        if name == "EditMessageText":
            return self._message(method.chat_id, text=method.text)
        if name == "SendPoll":
            self._poll_id += 1
            poll = Poll(
                id=f"poll{self._poll_id}", question=method.question,
                options=[PollOption(persistent_id=str(i), text=str(getattr(o, "text", o)), voter_count=0)
                         for i, o in enumerate(method.options)],
                total_voter_count=0, is_closed=False, is_anonymous=False, type="quiz",
                allows_multiple_answers=False, allows_revoting=False, members_only=False,
                correct_option_ids=method.correct_option_ids,
            )
            return self._message(method.chat_id, poll=poll)
        return True

    # ---- helpers for assertions
    def sent(self, kind: str = "SendMessage") -> list[Any]:
        return [c for c in self.calls if type(c).__name__ == kind]

    def texts(self) -> list[str]:
        return [c.text for c in self.calls if type(c).__name__ in ("SendMessage", "EditMessageText")]

    def last_text(self) -> str:
        return self.texts()[-1]

    def clear(self) -> None:
        self.calls.clear()

    def find_callback(self, prefix: str) -> str:
        """Most recent inline button whose callback data starts with ``prefix``."""
        for call in reversed(self.calls):
            markup = getattr(call, "reply_markup", None)
            for row in getattr(markup, "inline_keyboard", None) or []:
                for button in row:
                    if (button.callback_data or "").startswith(prefix):
                        return button.callback_data
        raise AssertionError(f"no button with callback prefix {prefix!r}")


# --------------------------------------------------------------------------- fake LLM
class FakeLLM:
    """Deterministic stand-in for LLMRouter: scripted tool calls + rule-based JSON."""

    def __init__(self) -> None:
        self.script: list[Any] = []     # items: ("tool", name, args) | ("text", str)
        self.fail = False
        self.chat_calls: list[list[dict]] = []
        self.json_calls: list[list[dict]] = []
        self.route_override: str | None = None

    configured = True

    def describe(self) -> dict[str, list[str]]:
        return {"smart": ["fake/smart"], "fast": ["fake/fast"]}

    async def aclose(self) -> None:
        return None

    async def chat(self, messages, *, tier="smart", tools=None, json_mode=False, temperature=0.3, max_tokens=None):  # noqa: ANN001
        if self.fail:
            raise LLMError("fake outage")
        self.chat_calls.append(messages)
        step = self.script.pop(0) if self.script else ("text", "Tushunarli.")
        if step[0] == "tool" and tools:
            call = {"id": f"call{len(self.chat_calls)}", "type": "function",
                    "function": {"name": step[1], "arguments": json.dumps(step[2])}}
            message = {"role": "assistant", "content": None, "tool_calls": [call], "_provider": "fake"}
            return LLMResult("", [call], message, "fake/smart")
        text = step[1] if step[0] == "text" else "Tayyor."
        return LLMResult(text, [], {"role": "assistant", "content": text, "_provider": "fake"}, "fake/smart")

    async def json(self, messages, *, tier="fast", temperature=0.1):  # noqa: ANN001
        if self.fail:
            raise LLMError("fake outage")
        self.json_calls.append(messages)
        system, user = messages[0]["content"], messages[-1]["content"]
        if "route messages" in system:
            return {"agent": self.route_override or heuristic_route(user.split("Message:", 1)[-1])}
        if "structured lesson report" in system:
            if re.search(r"eslat|uchrashuv", user):
                return {"is_lesson_report": False, "topic": "", "homework": None}
            match = re.search(r"(?:uyga|vazifa)[:,]?\s*(.+)$", user)
            homework = match.group(1).strip() if match and "yo'q" not in user else None
            topic = re.split(r",?\s*(?:uyga|vazifa)", user)[0].strip()
            return {"is_lesson_report": True, "topic": topic, "homework": homework}
        if "test savollari" in system:
            count = int(re.search(r"(\d+) ta savol tuz", user).group(1))
            return {"questions": [
                {"topic": f"Mavzu {i % 2 + 1}", "question": f"Savol {i + 1}: rekursiya nima?",
                 "options": [f"To'g'ri javob {i}", f"Xato A{i}", f"Xato B{i}", f"Xato C{i}"],
                 "correct_index": 0, "explanation": "Funksiya o'zini o'zi chaqiradi."}
                for i in range(count)
            ]}
        if "time expression" in system:
            return {"datetime": None}
        raise AssertionError(f"unexpected json prompt: {system[:80]}")


# --------------------------------------------------------------------------- fake Google
class FakeGoogle:
    def __init__(self) -> None:
        self.enabled = True
        self.fail = False
        self.events: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self._n = 0
        self.task_creates = 0

    async def aclose(self) -> None:
        return None

    def _check(self) -> None:
        if self.fail:
            raise GoogleError("HTTP 503: backend unavailable", 503)

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    # calendar
    async def list_events(self, start, end):  # noqa: ANN001
        self._check()
        out = []
        for event in self.events.values():
            s = timeutil.parse_iso_local(event["start"]["dateTime"])
            e = timeutil.parse_iso_local(event["end"]["dateTime"])
            if s < end and e > start:
                out.append(event)
        return sorted(out, key=lambda ev: ev["start"]["dateTime"])

    async def create_event(self, summary, start, end, location=None, description=None):  # noqa: ANN001
        self._check()
        event = {"id": self._id("evt"), "summary": summary, "location": location, "description": description,
                 "start": {"dateTime": timeutil.iso(start)}, "end": {"dateTime": timeutil.iso(end)},
                 "htmlLink": "https://calendar.google.com/event?eid=x"}
        self.events[event["id"]] = event
        return event

    async def update_event(self, event_id, **fields):  # noqa: ANN001, ANN003
        self._check()
        event = self.events[event_id]
        for key in ("summary", "location", "description"):
            if fields.get(key) is not None:
                event[key] = fields[key]
        if fields.get("start") is not None:
            event["start"] = {"dateTime": timeutil.iso(fields["start"])}
            event["end"] = {"dateTime": timeutil.iso(fields["end"])}
        return event

    async def delete_event(self, event_id):  # noqa: ANN001
        self._check()
        self.events.pop(event_id, None)

    # tasks
    async def list_tasks(self, updated_min=None):  # noqa: ANN001
        self._check()
        return list(self.tasks.values())

    async def create_task(self, title, notes, due):  # noqa: ANN001
        self._check()
        self.task_creates += 1
        task = {"id": self._id("gt"), "title": title, "notes": notes, "status": "needsAction",
                "due": f"{due.isoformat()}T00:00:00.000Z" if due else None}
        self.tasks[task["id"]] = task
        return task

    async def patch_task(self, task_id, body):  # noqa: ANN001
        self._check()
        if task_id not in self.tasks:
            raise GoogleError("HTTP 404", 404)
        self.tasks[task_id].update({k: v for k, v in body.items()})
        return self.tasks[task_id]

    async def delete_task(self, task_id):  # noqa: ANN001
        self._check()
        self.tasks.pop(task_id, None)


# --------------------------------------------------------------------------- clock
class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 11, 17, 7, 0, tzinfo=TZ)

    def set(self, value: str) -> datetime:
        self.current = datetime.fromisoformat(value).replace(tzinfo=TZ)
        return self.current


# --------------------------------------------------------------------------- fixtures
async def _db_ready() -> bool:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(TEST_DB)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def database():
    if not await _db_ready():
        pytest.skip("PostgreSQL test database is not reachable")
    from memora.db import dispose_engine, get_engine
    from memora.db.models import Base

    await dispose_engine()
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


@pytest_asyncio.fixture
async def env(database, monkeypatch):
    """Fresh database + fully faked outside world. Returns a namespace of handles."""
    from sqlalchemy import text

    from memora.bot.runner import build_dispatcher
    from memora.db import get_engine
    from memora.db.models import Base

    async with get_engine().begin() as conn:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))

    clock = Clock()
    monkeypatch.setattr(timeutil, "now", lambda: clock.current)

    get_settings.cache_clear()
    telegram = FakeTelegram()
    app = build_app(get_settings(), bot_session=telegram)
    app.llm = FakeLLM()
    app.google = FakeGoogle()

    csv_by_gid = {
        "1454027058": (FIXTURES / "sheet_degree.csv").read_text(newline=""),
        "1920812576": (FIXTURES / "sheet_english.csv").read_text(newline=""),
    }

    async def fake_fetch(url, client=None, attempts=3):  # noqa: ANN001
        gid = url.rsplit("gid=", 1)[-1]
        value = csv_by_gid[gid]
        if isinstance(value, Exception):
            raise value
        return value

    from memora.timetable import sync as sync_module

    monkeypatch.setattr(sync_module, "fetch_csv", fake_fetch)

    # Routers are module-level singletons, so the dispatcher is built once and re-pointed
    # at each test's App.
    global _DISPATCHER
    if _DISPATCHER is None:
        _DISPATCHER = build_dispatcher(app)
    dispatcher = _DISPATCHER
    dispatcher["app"] = app
    counter = {"update": 0}

    async def feed(payload: dict) -> None:
        counter["update"] += 1
        await dispatcher.feed_raw_update(app.bot, {"update_id": counter["update"], **payload})

    def _user(user_id: int = OWNER) -> dict:
        return {"id": user_id, "is_bot": False, "first_name": "Maxmanazar"}

    async def say(text_: str, user_id: int = OWNER, reply_to: int | None = None) -> None:
        message = {"message_id": 5000 + counter["update"], "date": int(clock.current.timestamp()),
                   "chat": {"id": user_id, "type": "private"}, "from": _user(user_id), "text": text_}
        if reply_to:
            message["reply_to_message"] = {"message_id": reply_to, "date": message["date"],
                                           "chat": message["chat"], "text": "…"}
        await feed({"message": message})

    async def press(data: str, user_id: int = OWNER, message_text: str = "…") -> None:
        await feed({"callback_query": {
            "id": str(counter["update"] + 1), "from": _user(user_id), "chat_instance": "ci", "data": data,
            "message": {"message_id": 7000 + counter["update"], "date": int(clock.current.timestamp()),
                        "chat": {"id": user_id, "type": "private"}, "text": message_text},
        }})

    async def voice() -> None:
        await feed({"message": {
            "message_id": 5000 + counter["update"], "date": int(clock.current.timestamp()),
            "chat": {"id": OWNER, "type": "private"}, "from": _user(),
            "voice": {"file_id": "voice-file", "file_unique_id": "u1", "duration": 4, "file_size": 9},
        }})

    async def answer_poll(poll_id: str, option: int, user_id: int = OWNER) -> None:
        await feed({"poll_answer": {"poll_id": poll_id, "user": _user(user_id), "option_ids": [option],
                                    "option_persistent_ids": [str(option)]}})

    class Env:
        pass

    handle = Env()
    handle.app, handle.tg, handle.llm, handle.google, handle.clock = app, telegram, app.llm, app.google, clock
    handle.say, handle.press, handle.answer_poll, handle.csv = say, press, answer_poll, csv_by_gid
    handle.tg_feed_voice = voice
    yield handle
    await app.bot.session.close()
