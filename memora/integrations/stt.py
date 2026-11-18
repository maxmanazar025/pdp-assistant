"""Speech-to-text for Telegram voice notes (Uzbek / Russian / English).

Telegram voice messages are OGG/Opus. They are normalised with ffmpeg to 16 kHz mono MP3,
a format every backend accepts, then sent down a fallback chain:

* ``gemini``  – native ``generateContent`` with inline audio (best Uzbek coverage);
* ``groq`` / ``openai`` – OpenAI-compatible ``/audio/transcriptions``.
"""
from __future__ import annotations

import asyncio
import base64
import logging

import httpx

from memora.config import Settings

log = logging.getLogger(__name__)

_PROMPT = (
    "Transcribe this voice message verbatim. The speaker is an Uzbek university student and may "
    "speak Uzbek, Russian or English, often mixing them with programming terms. Write Uzbek in "
    "Latin script, Russian in Cyrillic and English in English. Output ONLY the transcript, "
    "with no comments, translation or timestamps. If there is no intelligible speech output an "
    "empty string."
)


class STTError(RuntimeError):
    pass


async def to_mp3(data: bytes) -> bytes:
    """Convert any audio/video container to 16 kHz mono MP3 using ffmpeg."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", "-f", "mp3", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise STTError("ffmpeg topilmadi") from exc
    out, err = await asyncio.wait_for(proc.communicate(data), timeout=60)
    if proc.returncode != 0 or not out:
        raise STTError(f"ffmpeg xatosi: {err.decode(errors='replace')[:200]}")
    return out


class Transcriber:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._client = client or httpx.AsyncClient(timeout=90)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _backends(self) -> list[str]:
        available = {
            "gemini": bool(self._s.gemini_api_key),
            "groq": bool(self._s.groq_api_key),
            "openai": bool(self._s.openai_api_key),
        }
        return [b.strip() for b in self._s.stt_chain.split(",") if available.get(b.strip())]

    async def transcribe(self, audio: bytes) -> str:
        backends = self._backends()
        if not backends:
            raise STTError("Ovozni matnga o'girish sozlanmagan (GEMINI_API_KEY / GROQ_API_KEY / OPENAI_API_KEY)")
        mp3 = await to_mp3(audio)
        errors: list[str] = []
        for backend in backends:
            for attempt in range(2):
                try:
                    if backend == "gemini":
                        text = await self._gemini(mp3)
                    else:
                        text = await self._openai_compatible(backend, mp3)
                    return text.strip()
                except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                    errors.append(f"{backend}: {type(exc).__name__}: {str(exc)[:160]}")
                    log.warning("stt backend failed", extra={"backend": backend, "attempt": attempt})
                    retryable = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code in (429, 500, 502, 503, 504)
                    if not retryable:
                        break
                    await asyncio.sleep(0.8 * (attempt + 1))
        raise STTError("; ".join(errors))

    async def _gemini(self, mp3: bytes) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self._s.stt_gemini_model}:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [
                {"text": _PROMPT},
                {"inline_data": {"mime_type": "audio/mp3", "data": base64.b64encode(mp3).decode()}},
            ]}],
            "generationConfig": {"temperature": 0},
        }
        response = await self._client.post(url, headers={"x-goog-api-key": self._s.gemini_api_key}, json=body)
        response.raise_for_status()
        candidate = response.json()["candidates"][0]
        parts = candidate.get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))

    async def _openai_compatible(self, backend: str, mp3: bytes) -> str:
        base, key, model = {
            "groq": ("https://api.groq.com/openai/v1", self._s.groq_api_key, self._s.stt_groq_model),
            "openai": ("https://api.openai.com/v1", self._s.openai_api_key, self._s.stt_openai_model),
        }[backend]
        response = await self._client.post(
            f"{base}/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            data={"model": model, "response_format": "json", "temperature": "0"},
            files={"file": ("voice.mp3", mp3, "audio/mpeg")},
        )
        response.raise_for_status()
        return response.json()["text"]
