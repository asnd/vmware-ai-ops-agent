"""The orchestrator: source -> enrich -> LLM Gateway -> sink."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .config import Settings
from .enrich import Enricher
from .llm import LLMGatewayClient, LLMGatewayError, TTLCache
from .models import ProcessResult, Record
from .obs import PIPELINE_LATENCY, RECORDS, get_logger
from .sinks import build_sink
from .sources import build_source

log = get_logger(__name__)


class Pipeline:
    """Holds the long-lived clients so the HTTP server reuses connections."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enricher = Enricher(settings.enrichment)
        self.cache = TTLCache(settings.cache)
        self.llm = LLMGatewayClient(settings.llm_gateway, cache=self.cache)
        self.sink = build_sink(settings.sink)
        self._source = build_source(settings.source)

    async def aclose(self) -> None:
        await self.llm.aclose()

    async def fetch(self) -> list[Record]:
        records = await self._source.fetch()
        limit = self.settings.source.max_records
        if limit > 0 and len(records) > limit:
            log.info("pipeline.truncated_batch", fetched=len(records), limit=limit)
            records = records[:limit]
        RECORDS.labels("source", "ok").inc(len(records))
        return records

    async def process_record(
        self,
        record: Record,
        model: str | None = None,
        system_prompt: str | None = None,
        static_context: dict[str, Any] | None = None,
        publish: bool = True,
        bypass_cache: bool = False,
    ) -> ProcessResult:
        started = time.perf_counter()
        try:
            enriched = await self.enricher.enrich(
                record, static_override=static_context, system_override=system_prompt
            )
            RECORDS.labels("enrich", "ok").inc()
            llm_result = await self.llm.complete(enriched, model=model, bypass_cache=bypass_cache)
            result = ProcessResult(
                id=record.id,
                status="ok",
                origin=record.origin,
                request={
                    "model": model or self.settings.llm_gateway.model,
                    "system_prompt": enriched.system_prompt,
                    "user_prompt": enriched.user_prompt,
                    "truncated": enriched.truncated,
                },
                context=enriched.context,
                result=llm_result,
            )
        except (LLMGatewayError, ValueError, RuntimeError, KeyError) as exc:
            RECORDS.labels("pipeline", "error").inc()
            log.error(
                "pipeline.failed",
                record_id=record.id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return ProcessResult(id=record.id, status="error", origin=record.origin, error=str(exc))
        finally:
            PIPELINE_LATENCY.observe(time.perf_counter() - started)

        if publish:
            try:
                result.sink_ref = await self.sink.publish(result)
            except RuntimeError as exc:
                # The answer is good; only delivery failed — say so without losing it.
                log.error("pipeline.sink_failed", record_id=record.id, error=str(exc))
                result.status = "sink_error"
                result.error = str(exc)

        RECORDS.labels("pipeline", result.status).inc()
        return result

    async def process_batch(
        self,
        records: list[Record],
        concurrency: int = 4,
        **kwargs: Any,
    ) -> list[ProcessResult]:
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def run(record: Record) -> ProcessResult:
            async with semaphore:
                return await self.process_record(record, **kwargs)

        return list(await asyncio.gather(*(run(r) for r in records)))

    async def run_once(self, concurrency: int = 4) -> list[ProcessResult]:
        """Pull mode: fetch from the configured source and process everything."""
        records = await self.fetch()
        if not records:
            log.info("pipeline.no_records")
            return []
        log.info("pipeline.run_started", count=len(records))
        results = await self.process_batch(records, concurrency=concurrency)
        failed = sum(1 for r in results if r.status != "ok")
        log.info("pipeline.run_finished", count=len(results), failed=failed)
        return results
