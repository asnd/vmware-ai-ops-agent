"""Command line entry point: ``aigw serve | run-once | worker | validate``."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from .config import Settings, load_settings
from .obs import configure_logging, get_logger
from .pipeline import Pipeline
from .scheduler import run_scheduler

app = typer.Typer(add_completion=False, help="AI Gateway enrichment agent")
log = get_logger(__name__)

ConfigOption = Annotated[Path | None, typer.Option("--config", "-c", help="Path to config.yaml")]


def _load(config: Path | None) -> Settings:
    settings = load_settings(config)
    configure_logging(settings.observability.log_level, settings.observability.log_format)
    return settings


@app.command()
def serve(
    config: ConfigOption = None,
    host: Annotated[str | None, typer.Option(help="Override server.host")] = None,
    port: Annotated[int | None, typer.Option(help="Override server.port")] = None,
) -> None:
    """Run the HTTP server."""
    import uvicorn

    settings = _load(config)
    from .api import create_app

    uvicorn.run(
        create_app(settings),
        host=host or settings.server.host,
        port=port or settings.server.port,
        log_config=None,
    )


@app.command("run-once")
def run_once(
    config: ConfigOption = None,
    concurrency: Annotated[int, typer.Option(help="Records processed in parallel")] = 4,
    stdout: Annotated[bool, typer.Option(help="Also print results as JSON")] = False,
) -> None:
    """Pull from the source once, enrich, ask the gateway, publish to the sink."""
    settings = _load(config)
    pipeline = Pipeline(settings)

    async def main() -> list[dict]:
        try:
            results = await pipeline.run_once(concurrency=concurrency)
        finally:
            await pipeline.aclose()
        return [r.model_dump(mode="json") for r in results]

    payload = asyncio.run(main())
    if stdout:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    failed = sum(1 for r in payload if r["status"] != "ok")
    if failed:
        raise typer.Exit(code=1)


@app.command()
def worker(config: ConfigOption = None) -> None:
    """Run the periodic pull loop without the HTTP server."""
    settings = _load(config)
    pipeline = Pipeline(settings)

    async def main() -> None:
        try:
            await run_scheduler(pipeline, settings.scheduler)
        finally:
            await pipeline.aclose()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("worker.stopped")


@app.command()
def validate(config: ConfigOption = None) -> None:
    """Load the config, render the prompt template, and report what is wired up."""
    settings = _load(config)
    Pipeline(settings)  # Fails loudly on a bad template or unusable wiring.
    typer.echo(
        json.dumps(
            {
                "service": settings.service_name,
                "source": settings.source.type,
                "sink": settings.sink.type,
                "llm_gateway": settings.llm_gateway.chat_url,
                "model": settings.llm_gateway.model,
                "lookups": [lk.name for lk in settings.enrichment.lookups],
                "scheduler_enabled": settings.scheduler.enabled,
            },
            indent=2,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    app()
