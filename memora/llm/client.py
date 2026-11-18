"""Provider-agnostic LLM router.

Every supported provider is reached through the OpenAI-compatible Chat Completions
protocol, so one code path serves Gemini, OpenAI, Groq, OpenRouter or any self-hosted
gateway. Calls walk an ordered *chain* of ``provider/model`` routes:

* transient failures (429, 5xx, timeouts, connection errors) are retried on the same
  route with exponential backoff + jitter, honouring short ``Retry-After`` hints;
* when a route is exhausted, or fails with a non-retryable error, the next route in the
  chain takes over (model fallback);
* an exhausted route enters a short cool-down so subsequent requests skip straight to a
  healthy one instead of paying the retry latency again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from memora.config import Settings

log = logging.getLogger(__name__)

PROVIDER_BASE_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


class LLMError(RuntimeError):
    """All routes failed (or none are configured)."""


@dataclass(frozen=True, slots=True)
class Route:
    provider: str
    model: str
    base_url: str
    api_key: str

    @property
    def name(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(slots=True)
class LLMResult:
    content: str
    tool_calls: list[dict[str, Any]]
    message: dict[str, Any]          # raw assistant message, tagged with "_provider"
    route: str
    usage: dict[str, Any] = field(default_factory=dict)


def _provider_credentials(settings: Settings) -> dict[str, tuple[str, str]]:
    creds = {
        "gemini": (PROVIDER_BASE_URLS["gemini"], settings.gemini_api_key),
        "openai": (PROVIDER_BASE_URLS["openai"], settings.openai_api_key),
        "groq": (PROVIDER_BASE_URLS["groq"], settings.groq_api_key),
        "openrouter": (PROVIDER_BASE_URLS["openrouter"], settings.openrouter_api_key),
        "custom": (settings.llm_custom_base_url.rstrip("/"), settings.llm_custom_api_key),
    }
    return {name: (url, key) for name, (url, key) in creds.items() if url and key}


def build_chain(spec: str, settings: Settings) -> list[Route]:
    """'gemini/gemini-3.8-flash,groq/x' -> routes; providers without credentials are skipped."""
    creds = _provider_credentials(settings)
    routes: list[Route] = []
    for item in (part.strip() for part in spec.split(",")):
        if not item or "/" not in item:
            continue
        provider, model = item.split("/", 1)
        if provider in creds:
            base_url, key = creds[provider]
            routes.append(Route(provider, model, base_url, key))
    return routes


def parse_json_loose(text: str) -> Any:
    """Parse model output that should be JSON but may be fenced or wrapped in prose."""
    cleaned = _JSON_FENCE_RE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = min((i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0), default=-1)
        end = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if start < 0 or end <= start:
            raise
        return json.loads(cleaned[start:end + 1])


def _wire_messages(messages: list[dict[str, Any]], provider: str) -> list[dict[str, Any]]:
    """Prepare history for one provider.

    Assistant turns are replayed verbatim to the provider that produced them (Gemini 3
    requires its thought signatures back during tool use); for any other provider they
    are reduced to the portable OpenAI fields.
    """
    wire: list[dict[str, Any]] = []
    for msg in messages:
        origin = msg.get("_provider")
        clean = {k: v for k, v in msg.items() if k != "_provider"}
        if clean.get("role") == "assistant" and origin and origin != provider:
            portable: dict[str, Any] = {"role": "assistant", "content": clean.get("content") or ""}
            if clean.get("tool_calls"):
                portable["tool_calls"] = [
                    {"id": c.get("id"), "type": "function",
                     "function": {"name": c["function"]["name"], "arguments": c["function"].get("arguments") or "{}"}}
                    for c in clean["tool_calls"]
                ]
            clean = portable
        wire.append(clean)
    return wire


class LLMRouter:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=settings.llm_timeout_seconds)
        self._chains = {
            "smart": build_chain(settings.llm_smart_chain, settings),
            "fast": build_chain(settings.llm_fast_chain, settings),
        }
        self._cooldown: dict[str, float] = {}
        self._no_reasoning: set[str] = set()  # routes that rejected the reasoning_effort parameter
        self._sleep = asyncio.sleep  # patched in tests

    @property
    def configured(self) -> bool:
        return any(self._chains.values())

    def describe(self) -> dict[str, list[str]]:
        return {tier: [r.name for r in chain] for tier, chain in self._chains.items()}

    async def aclose(self) -> None:
        await self._client.aclose()

    def _ordered(self, tier: str) -> list[Route]:
        chain = self._chains.get(tier) or self._chains["smart"] or self._chains["fast"]
        if not chain:
            raise LLMError("LLM sozlanmagan: API kaliti yo'q (masalan GEMINI_API_KEY)")
        now = time.monotonic()
        healthy = [r for r in chain if self._cooldown.get(r.name, 0) <= now]
        cooling = [r for r in chain if r not in healthy]
        return healthy + cooling  # cooling routes are a last resort, never skipped entirely

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tier: str = "smart",
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> LLMResult:
        errors: list[str] = []
        for route in self._ordered(tier):
            try:
                return await self._call_route(route, messages, tools, json_mode, temperature, max_tokens)
            except _RouteFailed as exc:
                errors.append(f"{route.name}: {exc}")
                log.warning("llm route failed", extra={"route": route.name, "error": str(exc)})
        raise LLMError("; ".join(errors))

    async def json(self, messages: list[dict[str, Any]], *, tier: str = "fast", temperature: float = 0.1) -> Any:
        """Structured call. A reply that is not valid JSON is retried once with a nudge."""
        result = await self.chat(messages, tier=tier, json_mode=True, temperature=temperature)
        try:
            return parse_json_loose(result.content)
        except (json.JSONDecodeError, ValueError):
            retry = messages + [
                {"role": "assistant", "content": result.content},
                {"role": "user", "content": "Javob yaroqli JSON emas. Faqat bitta to'g'ri JSON obyekt qaytar."},
            ]
            result = await self.chat(retry, tier=tier, json_mode=True, temperature=0.0)
            return parse_json_loose(result.content)

    async def _call_route(
        self,
        route: Route,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        json_mode: bool,
        temperature: float,
        max_tokens: int | None,
    ) -> LLMResult:
        body: dict[str, Any] = {
            "model": route.model,
            "messages": _wire_messages(messages, route.provider),
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if self._settings.llm_reasoning_effort and route.name not in self._no_reasoning:
            body["reasoning_effort"] = self._settings.llm_reasoning_effort

        attempts = max(1, self._settings.llm_max_retries)
        last = "unknown error"
        for attempt in range(attempts):
            retry_after: float | None = None
            try:
                response = await self._client.post(
                    f"{route.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {route.api_key}"},
                    json=body,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    try:
                        payload = response.json()
                        message = payload["choices"][0]["message"]
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        last = f"malformed response: {exc}"
                    else:
                        content = message.get("content") or ""
                        if isinstance(content, list):  # some gateways return content parts
                            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
                        tool_calls = message.get("tool_calls") or []
                        if not content.strip() and not tool_calls:
                            last = "empty completion"
                        else:
                            message = {**message, "role": "assistant", "_provider": route.provider}
                            return LLMResult(content, tool_calls, message, route.name, payload.get("usage") or {})
                elif response.status_code in RETRYABLE_STATUS:
                    last = f"HTTP {response.status_code}: {response.text[:200]}"
                    header = response.headers.get("retry-after", "")
                    retry_after = float(header) if header.replace(".", "", 1).isdigit() else None
                    if retry_after is not None and retry_after > 10:
                        break  # provider asks for a long pause: fail over now
                elif response.status_code == 400 and "reasoning_effort" in body:
                    # This model does not accept the optional knob: drop it and try again.
                    self._no_reasoning.add(route.name)
                    del body["reasoning_effort"]
                    last = "reasoning_effort rejected"
                    continue
                else:
                    # 400/401/403/404: retrying the same request cannot help.
                    raise _RouteFailed(f"HTTP {response.status_code}: {response.text[:300]}")
            if attempt < attempts - 1:
                delay = retry_after if retry_after is not None else min(8.0, 0.6 * 2 ** attempt)
                await self._sleep(delay + random.uniform(0, 0.25))
        self._cooldown[route.name] = time.monotonic() + 60
        raise _RouteFailed(last)


class _RouteFailed(Exception):
    pass
