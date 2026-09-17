"""HTTP surface: synchronous processing, pull trigger, health and metrics."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .config import Settings, load_settings
from .models import BatchResponse, ProcessRequest, ProcessResult, Record
from .obs import configure_logging, get_logger
from .pipeline import Pipeline
from .scheduler import run_scheduler

log = get_logger(__name__)


def require_api_key(settings: Settings):
    """Inbound auth. When the configured env var is unset the API stays open."""

    async def _check(
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> None:
        env_name = settings.server.api_key_env
        expected = os.environ.get(env_name) if env_name else None
        if not expected:
            return
        presented = x_api_key or (
            authorization.removeprefix("Bearer ").strip() if authorization else None
        )
        if presented != expected:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key")

    return _check


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.observability.log_level, settings.observability.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.pipeline = Pipeline(settings)
        app.state.stop = asyncio.Event()
        app.state.worker = None
        if settings.scheduler.enabled:
            app.state.worker = asyncio.create_task(
                run_scheduler(app.state.pipeline, settings.scheduler, app.state.stop)
            )
            log.info("server.scheduler_started", interval=settings.scheduler.interval_seconds)
        log.info("server.started", service=settings.service_name, port=settings.server.port)
        try:
            yield
        finally:
            app.state.stop.set()
            if app.state.worker is not None:
                app.state.worker.cancel()
                try:
                    await app.state.worker
                except asyncio.CancelledError:
                    pass
            await app.state.pipeline.aclose()
            log.info("server.stopped")

    app = FastAPI(
        title=settings.service_name,
        version="0.1.0",
        description="Enrichment agent between an upstream data source and an LLM Gateway.",
        lifespan=lifespan,
    )
    if settings.server.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.server.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Best-effort: catches a declared Content-Length over the limit before we
        # read the body. A chunked request with no Content-Length slips through —
        # this is a sanity cap on well-behaved callers, not a hard guarantee.
        content_length = request.headers.get("content-length")
        if content_length is not None and content_length.isdigit():
            if int(content_length) > settings.server.max_body_bytes:
                return JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content={
                        "detail": (
                            f"request body of {content_length} bytes exceeds "
                            f"server.max_body_bytes ({settings.server.max_body_bytes})"
                        )
                    },
                )
        return await call_next(request)

    auth = Depends(require_api_key(settings))

    def pipeline() -> Pipeline:
        return app.state.pipeline

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": settings.service_name}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> dict[str, object]:
        """Ready means configured — it deliberately does not call the gateway."""
        return {
            "status": "ready",
            "llm_gateway": settings.llm_gateway.chat_url,
            "model": settings.llm_gateway.model,
            "source": settings.source.type,
            "sink": settings.sink.type,
            "scheduler": settings.scheduler.enabled,
        }

    @app.get("/metrics", tags=["ops"])
    async def metrics() -> Response:
        if not settings.observability.metrics_enabled:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "metrics disabled")
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/process", response_model=BatchResponse, tags=["pipeline"], dependencies=[auth])
    async def process(request: ProcessRequest) -> BatchResponse:
        """Enrich and answer. Inline payload/records, or pull from the source."""
        pipe = pipeline()
        if request.records is not None:
            records = [Record(payload=item, origin="inline") for item in request.records]
        elif request.payload is not None:
            records = [Record(payload=request.payload, origin="inline")]
        else:
            try:
                records = await pipe.fetch()
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller as 502
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"source failed: {exc}") from exc

        results = await pipe.process_batch(
            records,
            model=request.model,
            system_prompt=request.system_prompt,
            static_context=request.static_context,
            publish=request.publish,
            bypass_cache=request.bypass_cache,
        )
        return _summarise(results)

    @app.post("/v1/run", response_model=BatchResponse, tags=["pipeline"], dependencies=[auth])
    async def run_once() -> BatchResponse:
        """Trigger one full pull-enrich-answer-publish cycle on demand."""
        try:
            results = await pipeline().run_once()
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as 502
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"run failed: {exc}") from exc
        return _summarise(results)

    @app.get("/v1/config", tags=["ops"], dependencies=[auth])
    async def show_config() -> dict[str, object]:
        """Effective config. Secrets live in env vars, so only their names appear."""
        return settings.model_dump(mode="json")

    return app


def _summarise(results: list[ProcessResult]) -> BatchResponse:
    succeeded = sum(1 for r in results if r.status == "ok")
    return BatchResponse(
        count=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
        results=results,
    )
