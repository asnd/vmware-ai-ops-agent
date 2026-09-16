"""Client for an OpenAI-compatible LLM Gateway (LiteLLM, Portkey, Kong AI GW, vLLM...)."""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .config import CacheConfig, LLMGatewayConfig
from .models import EnrichedRecord, LLMResult, LLMUsage
from .obs import CACHE, LLM_LATENCY, LLM_REQUESTS, LLM_TOKENS, get_logger

log = get_logger(__name__)

# 408/409/429 and every 5xx are worth another attempt; 4xx otherwise is our bug.
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMGatewayError(RuntimeError):
    """The gateway refused or failed to answer."""


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


class TTLCache:
    """Small in-process prompt cache — keyed by model + messages, not by record id."""

    def __init__(self, cfg: CacheConfig) -> None:
        self.cfg = cfg
        self._items: OrderedDict[str, tuple[float, LLMResult]] = OrderedDict()

    @staticmethod
    def key(model: str, messages: list[dict[str, str]]) -> str:
        blob = json.dumps([model, messages], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, key: str) -> LLMResult | None:
        if not self.cfg.enabled:
            return None
        entry = self._items.get(key)
        if entry is None:
            CACHE.labels("miss").inc()
            return None
        expires_at, value = entry
        if expires_at < time.monotonic():
            del self._items[key]
            CACHE.labels("expired").inc()
            return None
        self._items.move_to_end(key)
        CACHE.labels("hit").inc()
        return value.model_copy(update={"cached": True})

    def put(self, key: str, value: LLMResult) -> None:
        if not self.cfg.enabled:
            return
        self._items[key] = (time.monotonic() + self.cfg.ttl_seconds, value)
        self._items.move_to_end(key)
        while len(self._items) > self.cfg.max_entries:
            self._items.popitem(last=False)


class LLMGatewayClient:
    def __init__(self, cfg: LLMGatewayConfig, cache: TTLCache | None = None) -> None:
        self.cfg = cfg
        self.cache = cache
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> LLMGatewayClient:
        self._client = httpx.AsyncClient(
            timeout=self.cfg.timeout_seconds, verify=self.cfg.verify_tls
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.cfg.timeout_seconds, verify=self.cfg.verify_tls
            )
        return self._client

    def build_payload(self, enriched: EnrichedRecord, model: str | None = None) -> dict[str, Any]:
        messages = []
        if enriched.system_prompt:
            messages.append({"role": "system", "content": enriched.system_prompt})
        messages.append({"role": "user", "content": enriched.user_prompt})
        payload: dict[str, Any] = {
            "model": model or self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            **self.cfg.extra_body,
        }
        if self.cfg.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def complete(
        self,
        enriched: EnrichedRecord,
        model: str | None = None,
        bypass_cache: bool = False,
    ) -> LLMResult:
        payload = self.build_payload(enriched, model)
        effective_model = payload["model"]
        cache_key = (
            TTLCache.key(effective_model, payload["messages"])
            if self.cache and not bypass_cache
            else None
        )
        if cache_key and self.cache:
            hit = self.cache.get(cache_key)
            if hit is not None:
                log.debug("llm.cache_hit", record_id=enriched.record.id)
                return hit

        headers = {
            "Content-Type": "application/json",
            # Correlation id the gateway can propagate into its own traces.
            "X-Request-Id": enriched.record.id,
            **self.cfg.extra_headers,
            **self.cfg.auth_headers(),
        }

        started = time.perf_counter()
        try:
            response = await self._send(payload, headers)
        except httpx.HTTPStatusError as exc:
            LLM_REQUESTS.labels(effective_model, f"http_{exc.response.status_code}").inc()
            raise LLMGatewayError(
                f"LLM Gateway returned {exc.response.status_code}: {exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            LLM_REQUESTS.labels(effective_model, "transport_error").inc()
            raise LLMGatewayError(f"LLM Gateway unreachable: {exc}") from exc

        latency = time.perf_counter() - started
        LLM_LATENCY.labels(effective_model).observe(latency)
        LLM_REQUESTS.labels(effective_model, "ok").inc()

        result = self._parse(response, latency_ms=latency * 1000)
        if result.gateway_meta:
            log.debug("llm.gateway_meta", record_id=enriched.record.id, **result.gateway_meta)
        if cache_key and self.cache:
            self.cache.put(cache_key, result)
        return result

    async def _send(self, payload: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        client = self._http()
        retrying = AsyncRetrying(
            stop=stop_after_attempt(max(1, self.cfg.max_retries)),
            wait=wait_exponential(
                multiplier=self.cfg.backoff_initial_seconds, max=self.cfg.backoff_max_seconds
            ),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                response = await client.post(self.cfg.chat_url, json=payload, headers=headers)
                response.raise_for_status()
                return response
        raise LLMGatewayError("unreachable")  # pragma: no cover - tenacity always returns

    def _parse(self, response: httpx.Response, latency_ms: float) -> LLMResult:
        try:
            body = response.json()
        except ValueError as exc:
            raise LLMGatewayError(f"gateway returned non-JSON: {response.text[:500]}") from exc
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMGatewayError(f"unexpected gateway response shape: {body}") from exc

        usage_raw = body.get("usage") or {}
        usage = LLMUsage(
            prompt_tokens=usage_raw.get("prompt_tokens"),
            completion_tokens=usage_raw.get("completion_tokens"),
            total_tokens=usage_raw.get("total_tokens"),
        )
        for kind, value in (
            ("prompt", usage.prompt_tokens),
            ("completion", usage.completion_tokens),
        ):
            if value:
                LLM_TOKENS.labels(kind).inc(value)

        parsed: Any | None = None
        if self.cfg.response_format == "json_object" and content:
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                log.warning("llm.json_parse_failed", preview=content[:200])

        gateway_meta = {
            name: response.headers[name]
            for name in self.cfg.capture_response_headers
            if name in response.headers
        }

        return LLMResult(
            content=content or "",
            model=body.get("model"),
            finish_reason=choice.get("finish_reason"),
            usage=usage,
            latency_ms=round(latency_ms, 2),
            parsed=parsed,
            gateway_meta=gateway_meta,
        )
