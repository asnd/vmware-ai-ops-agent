"""Wire and internal models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class Record(BaseModel):
    """One unit of work flowing through the pipeline."""

    id: str = Field(default_factory=new_id)
    payload: dict[str, Any] = Field(default_factory=dict)
    # Where the record came from, e.g. "http:https://inventory/api/v1/alerts".
    origin: str = "inline"
    received_at: datetime = Field(default_factory=_now)


class EnrichedRecord(BaseModel):
    record: Record
    # Everything the enrichers gathered: lookups keyed by name, computed fields.
    context: dict[str, Any] = Field(default_factory=dict)
    system_prompt: str = ""
    user_prompt: str = ""
    # Set when the prompt was clipped to prompt.max_input_chars.
    truncated: bool = False


class LLMUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class LLMResult(BaseModel):
    content: str
    model: str | None = None
    finish_reason: str | None = None
    usage: LLMUsage = Field(default_factory=LLMUsage)
    latency_ms: float = 0.0
    cached: bool = False
    # Whatever llm_gateway.capture_response_headers asked for.
    gateway_meta: dict[str, str] = Field(default_factory=dict)
    # Populated when response_format=json_object and the answer parsed cleanly.
    parsed: Any | None = None


class ProcessResult(BaseModel):
    id: str
    status: str = "ok"
    origin: str = "inline"
    created_at: datetime = Field(default_factory=_now)
    request: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    result: LLMResult | None = None
    error: str | None = None
    # Where the sink put it, when a sink ran.
    sink_ref: str | None = None


class ProcessRequest(BaseModel):
    """Body of ``POST /v1/process`` — inline payload, or nothing to pull the source."""

    payload: dict[str, Any] | None = None
    records: list[dict[str, Any]] | None = None
    # Per-request overrides.
    model: str | None = None
    system_prompt: str | None = None
    static_context: dict[str, Any] | None = None
    # When true the configured sink runs in addition to returning the body.
    publish: bool = False
    bypass_cache: bool = False


class BatchResponse(BaseModel):
    count: int
    succeeded: int
    failed: int
    results: list[ProcessResult]
