"""Periodic pull loop, used by ``aigw worker`` and by the server when enabled."""

from __future__ import annotations

import asyncio
import random

from .config import SchedulerConfig
from .obs import get_logger
from .pipeline import Pipeline

log = get_logger(__name__)


async def run_scheduler(
    pipeline: Pipeline, cfg: SchedulerConfig, stop: asyncio.Event | None = None
) -> None:
    stop = stop or asyncio.Event()
    if not cfg.run_on_start:
        await _sleep(cfg, stop)
    while not stop.is_set():
        try:
            await pipeline.run_once()
        except Exception as exc:  # noqa: BLE001 - the loop must survive any failure
            log.error("scheduler.cycle_failed", error=str(exc), error_type=type(exc).__name__)
        await _sleep(cfg, stop)


async def _sleep(cfg: SchedulerConfig, stop: asyncio.Event) -> None:
    delay = cfg.interval_seconds
    if cfg.jitter_seconds:
        # Spread restarts so a fleet of replicas does not stampede the source.
        delay += random.uniform(0, cfg.jitter_seconds)
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        return
