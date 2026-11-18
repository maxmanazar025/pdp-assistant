"""Intent router: picks the specialised agent for a free-form message.

A small/fast model classifies the message; if it is unavailable a multilingual keyword
heuristic takes over, so routing itself can never take the bot down.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from memora.llm.client import LLMError
from memora.services import state as state_service
from memora.timeparse import translit

log = logging.getLogger(__name__)

AGENT_NAMES = ("schedule", "tasks", "study", "general")

_SYSTEM = """You route messages for a student's personal assistant. The user writes Uzbek (Latin/Cyrillic), \
Russian or English, often mixed. Choose exactly one agent:

- "schedule": university timetable/classes, free time, Google Calendar events or meetings (view, create, move, delete).
- "tasks": to-dos, homework deadlines, reminders ("eslatib qo'y", "напомни", "remind me"), marking things done, \
snoozing, listing tasks.
- "study": explaining a concept, code help, study plans, exam prep, "test me"/quiz, anything about learning a subject.
- "general": greetings, thanks, small talk, questions about the assistant itself, anything else.

If the message is a short follow-up (e.g. "uni 5 ga sur", "ha, shuni", "yana bitta") keep the previous agent.
Reply with JSON only: {"agent": "<schedule|tasks|study|general>"}"""

_KEYWORDS = {
    "tasks": r"eslat|napomn|remind|vazifa|zadach|task|to-?do|bajarildi|bajardim|deadline|muddat|topshir|snooze|kechiktir",
    "schedule": r"jadval|dars\b|darsl|raspisan|schedule|para\b|uchrashuv|vstrech|meeting|tadbir|kalendar|calendar|"
                r"bo'sh vaqt|bo'shman|svobod|free time|qaysi xona|auditor",
    "study": r"tushuntir|obyasn|explain|o'rgat|qanday ishlaydi|misol|primer|example|kod|code|xato|error|bug|test qil|"
             r"quiz|savol ber|imtihon|ekzamen|exam|reja tuz|o'quv reja|study plan|takrorla|python|sql|java\b|algoritm",
}


def heuristic_route(text: str) -> str:
    s = translit(text)
    scores = {agent: len(re.findall(pattern, s)) for agent, pattern in _KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"


async def route(app, session: AsyncSession, text: str) -> str:  # noqa: ANN001
    previous = await state_service.last_domain(session)
    user = f"Previous agent: {previous or 'none'}\nMessage: {text[:1500]}"
    try:
        data = await app.llm.json(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}], tier="fast", temperature=0.0
        )
        agent = str(data.get("agent", "")).strip().lower() if isinstance(data, dict) else ""
        if agent in AGENT_NAMES:
            return agent
        log.warning("router returned unknown agent", extra={"raw": str(data)[:200]})
    except (LLMError, ValueError) as exc:
        log.warning("router LLM failed, using heuristic", extra={"error": str(exc)[:200]})
    guess = heuristic_route(text)
    return previous if guess == "general" and previous in AGENT_NAMES and len(text) < 40 else guess
