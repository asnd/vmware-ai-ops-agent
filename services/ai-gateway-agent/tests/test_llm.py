from __future__ import annotations

import httpx
import pytest
import respx

from aigw.config import CacheConfig, LLMGatewayConfig
from aigw.llm import LLMGatewayClient, LLMGatewayError, TTLCache
from aigw.models import EnrichedRecord, Record

URL = "http://gateway.test/v1/chat/completions"


def _enriched() -> EnrichedRecord:
    return EnrichedRecord(
        record=Record(id="r1", payload={}), system_prompt="sys", user_prompt="hello"
    )


def _cfg(**kwargs) -> LLMGatewayConfig:
    return LLMGatewayConfig(
        base_url="http://gateway.test/v1",
        model="test-model",
        max_retries=3,
        backoff_initial_seconds=0.0,
        backoff_max_seconds=0.0,
        **kwargs,
    )


@respx.mock
async def test_complete_parses_the_answer(chat_response, monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY_API_KEY", "gw-key")
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response))
    async with LLMGatewayClient(_cfg()) as client:
        result = await client.complete(_enriched())

    assert result.content == "all good"
    assert result.usage.total_tokens == 14
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer gw-key"
    # The record id is propagated so gateway traces line up with ours.
    assert request.headers["x-request-id"] == "r1"


@respx.mock
async def test_json_mode_parses_content(monkeypatch):
    monkeypatch.delenv("LLM_GATEWAY_API_KEY", raising=False)
    body = {"choices": [{"message": {"content": '{"severity": "high"}'}}]}
    respx.post(URL).mock(return_value=httpx.Response(200, json=body))
    async with LLMGatewayClient(_cfg(response_format="json_object")) as client:
        result = await client.complete(_enriched())
    assert result.parsed == {"severity": "high"}


@respx.mock
async def test_malformed_json_does_not_kill_the_answer():
    body = {"choices": [{"message": {"content": "not json"}}]}
    respx.post(URL).mock(return_value=httpx.Response(200, json=body))
    async with LLMGatewayClient(_cfg(response_format="json_object")) as client:
        result = await client.complete(_enriched())
    assert result.parsed is None
    assert result.content == "not json"


@respx.mock
async def test_retries_on_503_then_succeeds(chat_response):
    route = respx.post(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=chat_response)]
    )
    async with LLMGatewayClient(_cfg()) as client:
        result = await client.complete(_enriched())
    assert result.content == "all good"
    assert route.call_count == 2


@respx.mock
async def test_client_errors_are_not_retried():
    route = respx.post(URL).mock(return_value=httpx.Response(400, text="bad prompt"))
    async with LLMGatewayClient(_cfg()) as client:
        with pytest.raises(LLMGatewayError, match="returned 400"):
            await client.complete(_enriched())
    assert route.call_count == 1


@respx.mock
async def test_unexpected_shape_is_reported():
    respx.post(URL).mock(return_value=httpx.Response(200, json={"nope": True}))
    async with LLMGatewayClient(_cfg()) as client:
        with pytest.raises(LLMGatewayError, match="unexpected gateway response shape"):
            await client.complete(_enriched())


@respx.mock
async def test_cache_short_circuits_identical_prompts(chat_response):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response))
    cache = TTLCache(CacheConfig(enabled=True, ttl_seconds=60))
    async with LLMGatewayClient(_cfg(), cache=cache) as client:
        first = await client.complete(_enriched())
        second = await client.complete(_enriched())
        third = await client.complete(_enriched(), bypass_cache=True)

    assert route.call_count == 2
    assert not first.cached and second.cached and not third.cached


def test_cache_evicts_the_oldest_entry():
    cache = TTLCache(CacheConfig(enabled=True, ttl_seconds=60, max_entries=1))
    from aigw.models import LLMResult

    cache.put("a", LLMResult(content="a"))
    cache.put("b", LLMResult(content="b"))
    assert cache.get("a") is None
    assert cache.get("b") is not None
