"""Lesson Reflection Extractor and small structured-extraction helpers."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from memora import timeutil
from memora.llm.client import LLMError
from memora.textfmt import truncate
from memora.timeutil import WEEKDAYS_UZ

log = logging.getLogger(__name__)

_SYSTEM = """You extract a structured lesson report from a student's short message (typed or transcribed from a \
voice note right after class). The message may be in Uzbek (Latin/Cyrillic), Russian or English, messy and mixed.

The class that just ended: "{subject}".

Return JSON only:
{{"is_lesson_report": true|false, "topic": "...", "homework": "..." | null}}

Rules:
- "is_lesson_report" is false ONLY if the message is clearly about something else (a new command, a reminder \
request, a question unrelated to this class). A short answer like "funksiyalar, vazifa yo'q" IS a report.
- "topic": what was covered, rewritten as a clear concise Uzbek (Latin script) phrase or sentence. Keep technical \
terms in English. Do not invent details that were not said.
- "homework": the assigned homework rewritten in clear Uzbek (Latin), including any numbers, pages or deadlines \
mentioned. Use null if no homework was given or it was not mentioned ("vazifa yo'q", "berilmadi", "ничего не задали").
- Never translate code identifiers or names."""

_TIME_SYSTEM = """Convert the user's time expression to one concrete local datetime.
Now: {now} ({weekday}), timezone Asia/Tashkent.
Date table:
{table}
Uzbek words: bugun=today, ertaga=tomorrow, indinga=day after tomorrow, ertalab=morning, kechqurun=evening (19:00 \
if no hour). A bare hour 1-7 means afternoon. Weekday names mean the next such day.
Return JSON only: {{"datetime": "YYYY-MM-DDTHH:MM"}} or {{"datetime": null}} if no time is expressed."""


@dataclass(frozen=True, slots=True)
class Reflection:
    relevant: bool
    topic: str
    homework: str | None
    degraded: bool = False  # True when the LLM was unavailable and the raw text was stored


async def extract_reflection(app, text: str, subject: str) -> Reflection:  # noqa: ANN001
    try:
        data = await app.llm.json(
            [{"role": "system", "content": _SYSTEM.format(subject=subject)}, {"role": "user", "content": text[:4000]}],
            tier="fast", temperature=0.0,
        )
    except (LLMError, ValueError) as exc:
        # Never lose a lesson report because a model is down: keep the student's own words.
        log.warning("reflection extraction degraded", extra={"error": str(exc)[:200]})
        return Reflection(True, truncate(text, 1000), None, degraded=True)
    if not isinstance(data, dict):
        return Reflection(True, truncate(text, 1000), None, degraded=True)
    topic = str(data.get("topic") or "").strip()
    homework = data.get("homework")
    homework = str(homework).strip() if homework and str(homework).strip().lower() not in ("null", "none", "yo'q") else None
    relevant = bool(data.get("is_lesson_report", True)) and bool(topic)
    return Reflection(relevant, topic or truncate(text, 1000), homework)


async def resolve_time_llm(app, text: str) -> datetime | None:  # noqa: ANN001
    """LLM fallback for time phrases the deterministic parser could not resolve."""
    now = timeutil.now()
    try:
        data = await app.llm.json(
            [
                {"role": "system", "content": _TIME_SYSTEM.format(
                    now=timeutil.iso(now), weekday=WEEKDAYS_UZ[now.weekday()], table=timeutil.calendar_table(10))},
                {"role": "user", "content": text[:500]},
            ],
            tier="fast", temperature=0.0,
        )
        value = data.get("datetime") if isinstance(data, dict) else None
        return timeutil.parse_iso_local(value) if value else None
    except (LLMError, ValueError, TypeError):
        return None
