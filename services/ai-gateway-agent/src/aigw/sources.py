"""Input adapters: pull records from another microservice or from a local file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

import httpx

from .config import FileSourceConfig, HttpSourceConfig, SourceConfig
from .models import Record
from .obs import get_logger

log = get_logger(__name__)


def dig(data: Any, path: str | None) -> Any:
    """Walk a dotted path (``data.items.0``) through dicts and lists."""
    if not path:
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise KeyError(f"cannot resolve '{path}' at segment '{part}'") from exc
        elif isinstance(current, dict):
            if part not in current:
                raise KeyError(f"cannot resolve '{path}': missing segment '{part}'")
            current = current[part]
        else:
            raise KeyError(f"cannot resolve '{path}': segment '{part}' is not traversable")
    return current


def to_records(data: Any, origin: str) -> list[Record]:
    """Normalise whatever the source returned into a list of records."""
    if data is None:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        # A scalar or a plain string still deserves to be processed.
        data = [{"value": data}]
    records: list[Record] = []
    for item in data:
        payload = item if isinstance(item, dict) else {"value": item}
        # Honour an id the upstream system already assigned.
        rid = payload.get("id") or payload.get("uuid")
        record = Record(payload=payload, origin=origin)
        if isinstance(rid, (str, int)):
            record.id = str(rid)
        records.append(record)
    return records


class Source(Protocol):
    async def fetch(self) -> list[Record]: ...


class FileSource:
    def __init__(self, cfg: FileSourceConfig) -> None:
        self.cfg = cfg

    async def fetch(self) -> list[Record]:
        path = Path(self.cfg.path)
        if not path.is_file():
            raise FileNotFoundError(f"source file not found: {path}")
        raw = path.read_text(encoding=self.cfg.encoding)
        origin = f"file:{path}"
        if self.cfg.format == "text":
            return to_records({"text": raw}, origin)
        if self.cfg.format == "jsonl":
            items = [json.loads(line) for line in raw.splitlines() if line.strip()]
            return to_records(items, origin)
        return to_records(dig(json.loads(raw), self.cfg.records_path), origin)


class HttpSource:
    """Pulls from a sibling microservice's REST API."""

    def __init__(self, cfg: HttpSourceConfig) -> None:
        self.cfg = cfg

    async def fetch(self) -> list[Record]:
        headers = {**self.cfg.headers, **self.cfg.auth.headers()}
        async with httpx.AsyncClient(
            timeout=self.cfg.timeout_seconds, verify=self.cfg.verify_tls
        ) as client:
            response = await client.request(
                self.cfg.method,
                self.cfg.url,
                headers=headers,
                params=self.cfg.params or None,
                json=self.cfg.body if self.cfg.method == "POST" else None,
            )
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError:
                data = {"text": response.text}
        log.debug("source.fetched", url=self.cfg.url, status=response.status_code)
        return to_records(dig(data, self.cfg.records_path), f"http:{self.cfg.url}")


def build_source(cfg: SourceConfig) -> Source:
    return HttpSource(cfg.http) if cfg.type == "http" else FileSource(cfg.file)
