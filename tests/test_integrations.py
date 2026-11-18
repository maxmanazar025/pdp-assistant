"""Google client, speech-to-text and the voice-to-action pipeline."""
from __future__ import annotations

import asyncio
import json
import shutil
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from memora.config import Settings
from memora.db import session_scope
from memora.db.models import LessonLog
from memora.integrations.google import GoogleAuthError, GoogleClient, parse_event_times
from memora.integrations.stt import Transcriber, to_mp3
from memora.timetable.sync import sync_timetable
from memora.worker import jobs

TZ = ZoneInfo("Asia/Tashkent")


def _google(handler) -> GoogleClient:
    settings = Settings(_env_file=None, google_client_id="id", google_client_secret="secret",
                        google_refresh_token="refresh")
    return GoogleClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_google_token_refresh_and_stale_token_retry():
    log: list[str] = []
    tokens = iter(["tok-1", "tok-2"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            log.append("token")
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
        log.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer tok-1" and log.count("Bearer tok-1") == 2:
            return httpx.Response(401, text="expired")
        return httpx.Response(200, json={"items": []})

    client = _google(handler)
    await client.list_tasks()
    await client.list_tasks()                                    # 401 -> refresh -> retry
    assert log == ["token", "Bearer tok-1", "Bearer tok-1", "token", "Bearer tok-2"]


async def test_google_revoked_refresh_token_raises_auth_error():
    client = _google(lambda request: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(GoogleAuthError):
        await client.list_tasks()


async def test_google_request_shapes():
    seen: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else {}))
        return httpx.Response(200, json={"id": "x"})

    client = _google(handler)
    await client.create_event("Uchrashuv", datetime(2026, 11, 18, 15, 0, tzinfo=TZ),
                              datetime(2026, 11, 18, 16, 0, tzinfo=TZ), location="Kutubxona")
    await client.create_task("Vazifa", "izoh", date(2026, 11, 20))
    method, path, body = seen[0]
    assert (method, path) == ("POST", "/calendar/v3/calendars/primary/events")
    assert body["start"] == {"dateTime": "2026-11-18T15:00:00+05:00", "timeZone": "Asia/Tashkent"}
    method, path, body = seen[1]
    assert (method, path) == ("POST", "/tasks/v1/lists/@default/tasks")
    assert body == {"title": "Vazifa", "notes": "izoh", "due": "2026-11-20T00:00:00.000Z"}


def test_parse_event_times_all_day_and_timed():
    start, end, all_day = parse_event_times({"start": {"date": "2026-11-18"}, "end": {"date": "2026-11-19"}})
    assert all_day and (end - start).days == 1
    start, end, all_day = parse_event_times({"start": {"dateTime": "2026-11-18T10:00:00Z"},
                                             "end": {"dateTime": "2026-11-18T11:00:00Z"}})
    assert not all_day and start.hour == 15                      # converted to Asia/Tashkent


async def _ogg_opus() -> bytes:
    """A real Telegram-style voice container: 1 s of tone as OGG/Opus."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:a", "libopus", "-f", "ogg", "pipe:1", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    return out


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_stt_converts_opus_and_falls_back_between_backends():
    voice = await _ogg_opus()
    if not voice:
        pytest.skip("ffmpeg build has no libopus encoder")
    mp3 = await to_mp3(voice)
    assert mp3[:3] == b"ID3" or mp3[0] == 0xFF                   # MP3 container/frame header

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "generativelanguage.googleapis.com":
            body = json.loads(request.content)
            assert body["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "audio/mp3"
            assert request.headers["x-goog-api-key"] == "g"
            return httpx.Response(400, text="unsupported")       # non-retryable -> next backend
        assert request.url.path == "/openai/v1/audio/transcriptions"
        return httpx.Response(200, json={"text": " Ertaga soat uchda eslat "})

    settings = Settings(_env_file=None, gemini_api_key="g", groq_api_key="q")
    stt = Transcriber(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await stt.transcribe(voice) == "Ertaga soat uchda eslat"
    assert seen == ["generativelanguage.googleapis.com", "api.groq.com"]


async def test_voice_note_after_class_becomes_lesson_log(env):
    """Voice-to-action: a voice message in awaiting_lesson_log is transcribed and logged."""
    await sync_timetable(env.app, announce=False)
    await jobs.tick(env.app, env.clock.set("2026-11-17T12:20"))

    class FakeSTT:
        async def transcribe(self, audio: bytes) -> str:
            assert audio == b"OGG-BYTES"
            return "Dekoratorlar o'tildi, uyga kitobdan 3-bobni o'qish"

        async def aclose(self) -> None:
            return None

    env.app.stt = FakeSTT()

    async def fake_download(file_id, destination=None, **kwargs):  # noqa: ANN001, ANN003
        destination.write(b"OGG-BYTES")
        return destination

    env.app.bot.download = fake_download
    env.tg.clear()
    await env.tg_feed_voice()
    texts = env.tg.texts()
    assert any(t.startswith("🎙 <i>Dekoratorlar o'tildi") for t in texts)   # transcript echoed back
    assert any("Qayd etildi" in t for t in texts)
    async with session_scope() as session:
        log = (await session.execute(select(LessonLog))).scalar_one()
    assert log.input_kind == "voice" and log.topic == "Dekoratorlar o'tildi" and log.homework == "kitobdan 3-bobni o'qish"
