"""structlog + Prometheus wiring."""

from __future__ import annotations

import logging
import sys

import structlog
from prometheus_client import Counter, Histogram

RECORDS = Counter("aigw_records_total", "Records processed by the pipeline", ["stage", "status"])
LLM_REQUESTS = Counter(
    "aigw_llm_requests_total", "Requests sent to the LLM Gateway", ["model", "status"]
)
LLM_LATENCY = Histogram(
    "aigw_llm_latency_seconds",
    "LLM Gateway round-trip latency",
    ["model"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40, 80),
)
LLM_TOKENS = Counter("aigw_llm_tokens_total", "Tokens reported by the gateway", ["kind"])
PIPELINE_LATENCY = Histogram(
    "aigw_pipeline_latency_seconds",
    "End-to-end latency per record",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
CACHE = Counter("aigw_cache_total", "Prompt cache outcomes", ["outcome"])
SINK = Counter("aigw_sink_total", "Sink deliveries", ["type", "status"])


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    # httpx logs every request at INFO, which duplicates our own structured events.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    renderer = (
        structlog.processors.JSONRenderer() if fmt == "json" else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "aigw") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
