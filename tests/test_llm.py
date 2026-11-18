"""LLM router resilience: retries with backoff, model fallback, cool-down, portable history."""
from __future__ import annotations

import json

import httpx
import pytest

from memora.config import Settings
from memora.llm.client import LLMError, LLMRouter, _wire_messages, build_chain, parse_json_loose


def _ok(text: str = "salom", tool_calls: list | None = None) -> httpx.Response:
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": message}], "usage": {"total_tokens": 7}})


def _router(handler, **overrides) -> tuple[LLMRouter, list[float]]:
    settings = Settings(
        _env_file=None, gemini_api_key="g-key", groq_api_key="q-key",
        llm_smart_chain="gemini/primary,gemini/secondary,groq/backup,openai/never-configured",
        llm_fast_chain="gemini/lite", **overrides,
    )
    router = LLMRouter(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    router._sleep = fake_sleep
    return router, sleeps


def test_chain_skips_providers_without_credentials():
    settings = Settings(_env_file=None, gemini_api_key="k", llm_smart_chain="openai/a,gemini/b, groq/c ,bad")
    assert [r.name for r in build_chain(settings.llm_smart_chain, settings)] == ["gemini/b"]


async def test_rate_limit_is_retried_with_backoff_then_succeeds():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(429, text="slow down") if len(calls) < 3 else _ok()

    router, sleeps = _router(handler)
    result = await router.chat([{"role": "user", "content": "hi"}])
    assert result.content == "salom" and result.route == "gemini/primary"
    assert calls == ["primary"] * 3
    assert len(sleeps) == 2 and sleeps[0] < sleeps[1]            # exponential


async def test_falls_back_across_models_and_providers():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen.append((request.url.host, model, request.headers["authorization"]))
        if model == "primary":
            return httpx.Response(503, text="overloaded")
        if model == "secondary":
            raise httpx.ConnectTimeout("timeout")
        return _ok("zaxira javob")

    router, _ = _router(handler)
    result = await router.chat([{"role": "user", "content": "hi"}])
    assert result.route == "groq/backup" and result.content == "zaxira javob"
    assert seen[-1] == ("api.groq.com", "backup", "Bearer q-key")
    assert [m for _, m, _ in seen] == ["primary"] * 3 + ["secondary"] * 3 + ["backup"]

    # Exhausted routes are cooling down: the next call goes straight to the healthy one.
    seen.clear()
    await router.chat([{"role": "user", "content": "again"}])
    assert [m for _, m, _ in seen] == ["backup"]


async def test_non_retryable_error_fails_over_immediately():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen.append(model)
        return httpx.Response(400, text="bad model") if model == "primary" else _ok()

    router, sleeps = _router(handler)
    assert (await router.chat([{"role": "user", "content": "x"}])).route == "gemini/secondary"
    assert seen == ["primary", "secondary"] and sleeps == []


async def test_all_routes_down_raises_llm_error():
    router, _ = _router(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(LLMError) as err:
        await router.chat([{"role": "user", "content": "x"}])
    assert "gemini/primary" in str(err.value) and "groq/backup" in str(err.value)


async def test_long_retry_after_skips_to_next_route():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen.append(model)
        return httpx.Response(429, headers={"retry-after": "3600"}) if model == "primary" else _ok()

    router, sleeps = _router(handler)
    await router.chat([{"role": "user", "content": "x"}])
    assert seen == ["primary", "secondary"] and sleeps == []


async def test_json_mode_repairs_invalid_json_once():
    replies = iter(["mana javob: {agent: schedule", '```json\n{"agent": "schedule"}\n```'])
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _ok(next(replies))

    router, _ = _router(handler)
    assert await router.json([{"role": "user", "content": "x"}]) == {"agent": "schedule"}
    assert bodies[0]["response_format"] == {"type": "json_object"} and bodies[0]["model"] == "lite"
    assert len(bodies) == 2 and "yaroqli JSON emas" in bodies[1]["messages"][-1]["content"]


def test_parse_json_loose():
    assert parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_loose('Natija: {"a": [1, 2]} tamom') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        parse_json_loose("json yo'q")


def test_history_is_verbatim_for_origin_provider_and_portable_for_others():
    call = {"id": "c1", "type": "function", "function": {"name": "get_schedule", "arguments": "{}"},
            "extra_content": {"google": {"thought_signature": "sig"}}}
    history = [
        {"role": "user", "content": "jadval"},
        {"role": "assistant", "content": None, "tool_calls": [call], "_provider": "gemini"},
        {"role": "tool", "tool_call_id": "c1", "name": "get_schedule", "content": "{}"},
    ]
    same = _wire_messages(history, "gemini")
    assert same[1]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "sig"
    assert "_provider" not in same[1]
    other = _wire_messages(history, "groq")
    assert "extra_content" not in other[1]["tool_calls"][0] and other[1]["content"] == ""
    assert other[1]["tool_calls"][0]["function"]["name"] == "get_schedule"


async def test_reasoning_effort_is_dropped_for_models_that_reject_it():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(400, text="unknown field reasoning_effort") if "reasoning_effort" in body else _ok()

    router, sleeps = _router(handler, llm_reasoning_effort="low")
    assert (await router.chat([{"role": "user", "content": "x"}])).route == "gemini/primary"
    await router.chat([{"role": "user", "content": "y"}])
    assert ["reasoning_effort" in b for b in bodies] == [True, False, False] and sleeps == []
