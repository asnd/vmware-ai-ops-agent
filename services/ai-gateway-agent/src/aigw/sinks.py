"""Output adapters: write the answer to disk or push it to the next service."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from .config import FileSinkConfig, HttpSinkConfig, SinkConfig
from .llm import RETRYABLE_STATUS
from .models import ProcessResult
from .obs import SINK, get_logger

log = get_logger(__name__)


class Sink(Protocol):
    async def publish(self, result: ProcessResult) -> str | None: ...


class NullSink:
    """Used by the synchronous API, where the HTTP caller is the consumer."""

    async def publish(self, result: ProcessResult) -> str | None:
        return None


class FileSink:
    def __init__(self, cfg: FileSinkConfig) -> None:
        self.cfg = cfg
        self._append_lock = asyncio.Lock()

    def _target(self, result: ProcessResult) -> Path:
        now = datetime.now(UTC)
        name = self.cfg.filename_template.format(
            id=result.id,
            ts=int(now.timestamp()),
            date=now.strftime("%Y-%m-%d"),
        )
        # A template with slashes creates the nested directories.
        return Path(self.cfg.path) / name

    async def publish(self, result: ProcessResult) -> str | None:
        try:
            target = self._target(result)
        except (KeyError, IndexError) as exc:
            # A typo'd placeholder in filename_template — this record's problem,
            # not the whole batch's.
            SINK.labels("file", "error").inc()
            raise RuntimeError(f"file sink filename_template is invalid: {exc}") from exc
        # Stamp the destination before serialising so the stored record is self-describing.
        result.sink_ref = str(target)
        body = result.model_dump_json(indent=2)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write then rename so a reader never sees a half-written file.
            tmp = target.with_suffix(target.suffix + ".tmp")
            await asyncio.to_thread(tmp.write_text, body, encoding="utf-8")
            await asyncio.to_thread(tmp.replace, target)
            if self.cfg.append_jsonl:
                await self._append(result)
        except OSError as exc:
            SINK.labels("file", "error").inc()
            raise RuntimeError(f"file sink failed for {target}: {exc}") from exc
        SINK.labels("file", "ok").inc()
        log.info("sink.written", path=str(target), record_id=result.id)
        return str(target)

    async def _append(self, result: ProcessResult) -> None:
        line = json.dumps(result.model_dump(mode="json"), ensure_ascii=False) + "\n"
        path = Path(self.cfg.append_jsonl or "")

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)

        # Serialised so concurrent records cannot interleave inside one line.
        async with self._append_lock:
            await asyncio.to_thread(_write)


class HttpSink:
    """Pushes the answer to the downstream microservice (webhook style)."""

    def __init__(self, cfg: HttpSinkConfig) -> None:
        self.cfg = cfg

    async def publish(self, result: ProcessResult) -> str | None:
        headers = {
            "Content-Type": "application/json",
            "X-Request-Id": result.id,
            **self.cfg.headers,
            **self.cfg.auth.headers(),
        }
        result.sink_ref = self.cfg.url
        body = result.model_dump(mode="json")
        last_error: Exception | None = None
        async with httpx.AsyncClient(
            timeout=self.cfg.timeout_seconds, verify=self.cfg.verify_tls
        ) as client:
            for attempt in range(1, max(1, self.cfg.max_retries) + 1):
                try:
                    response = await client.request(
                        self.cfg.method, self.cfg.url, json=body, headers=headers
                    )
                    response.raise_for_status()
                    SINK.labels("http", "ok").inc()
                    log.info("sink.posted", url=self.cfg.url, record_id=result.id)
                    return self.cfg.url
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    if exc.response.status_code not in RETRYABLE_STATUS:
                        # A permanent rejection (bad payload, auth, wrong path) —
                        # retrying it burns attempts on something that can never work.
                        log.warning(
                            "sink.non_retryable",
                            url=self.cfg.url,
                            status=exc.response.status_code,
                        )
                        break
                except httpx.HTTPError as exc:
                    last_error = exc

                log.warning("sink.retry", url=self.cfg.url, attempt=attempt, error=str(last_error))
                if attempt < max(1, self.cfg.max_retries):
                    await asyncio.sleep(min(2 ** (attempt - 1) * 0.5, 8))
        SINK.labels("http", "error").inc()
        raise RuntimeError(f"http sink failed for {self.cfg.url}: {last_error}")


def build_sink(cfg: SinkConfig) -> Sink:
    if cfg.type == "file":
        return FileSink(cfg.file)
    if cfg.type == "http":
        return HttpSink(cfg.http)
    return NullSink()
