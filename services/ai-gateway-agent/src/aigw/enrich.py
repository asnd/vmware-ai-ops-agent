"""The agent step: turn a raw record into a grounded prompt.

This is the Content Enricher of the pipeline — it pulls in whatever extra context
the LLM needs (sibling-service lookups, static facts), strips what must never
leave the perimeter, and renders the final prompt from a template.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from jinja2 import Environment, StrictUndefined, TemplateError

from .config import EnrichmentConfig, LookupConfig
from .models import EnrichedRecord, Record
from .obs import get_logger
from .sources import dig

log = get_logger(__name__)

# `undefined=StrictUndefined` so a typo in the template fails loudly at startup-ish
# time instead of silently sending an empty prompt to the gateway.
_JINJA = Environment(undefined=StrictUndefined, autoescape=False, enable_async=False)

REDACTED = "***redacted***"


def redact(payload: dict[str, Any], redact_fields: list[str]) -> dict[str, Any]:
    """Mask redact_fields only — every other field survives, no include_fields filter."""
    data = dict(payload)
    for field in redact_fields:
        if field in data:
            data[field] = REDACTED
    return data


def project(payload: dict[str, Any], cfg: EnrichmentConfig) -> dict[str, Any]:
    """Apply the include/redact policy to a record payload."""
    data = dict(payload)
    if cfg.include_fields:
        data = {k: v for k, v in data.items() if k in cfg.include_fields}
    return redact(data, cfg.redact_fields)


async def run_lookup(
    client: httpx.AsyncClient, lookup: LookupConfig, record: Record
) -> tuple[str, Any]:
    """Resolve one lookup. URLs may reference the record: ``/hosts/{id}``."""
    # record.id last: it always wins over a same-named payload field (they agree
    # when the upstream record carried its own "id", so this is never a surprise)
    # and a single merged dict avoids passing "id" both positionally and via **.
    fields = {**record.payload, "id": record.id}
    try:
        url = lookup.url.format(**fields)
    except (KeyError, IndexError):
        # A placeholder the record cannot satisfy — send the URL as written.
        url = lookup.url
    response = await client.request(
        lookup.method,
        url,
        headers={**lookup.headers, **lookup.auth.headers()},
        timeout=lookup.timeout_seconds,
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError:
        data = response.text
    return lookup.name, dig(data, lookup.records_path)


class Enricher:
    def __init__(self, cfg: EnrichmentConfig) -> None:
        self.cfg = cfg
        self._template = _JINJA.from_string(cfg.prompt.template)
        self._system = _JINJA.from_string(cfg.prompt.system)

    async def gather_context(self, record: Record) -> dict[str, Any]:
        if not self.cfg.lookups:
            return {}
        context: dict[str, Any] = {}
        # One client per distinct verify_tls setting — usually just one — so an
        # internal lookup with its own CA can opt out of verification without
        # weakening the others.
        clients: dict[bool, httpx.AsyncClient] = {}

        async def run(lookup: LookupConfig) -> tuple[str, Any]:
            client = clients.setdefault(
                lookup.verify_tls, httpx.AsyncClient(verify=lookup.verify_tls)
            )
            return await run_lookup(client, lookup, record)

        try:
            tasks = [run(lk) for lk in self.cfg.lookups]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await asyncio.gather(*(c.aclose() for c in clients.values()))
        for lookup, outcome in zip(self.cfg.lookups, results, strict=True):
            if isinstance(outcome, BaseException):
                if not lookup.optional:
                    raise RuntimeError(f"lookup '{lookup.name}' failed: {outcome}") from outcome
                log.warning(
                    "enrich.lookup_failed",
                    lookup=lookup.name,
                    record_id=record.id,
                    error=str(outcome),
                )
                context[lookup.name] = None
                continue
            name, value = outcome
            context[name] = value
        return context

    async def enrich(
        self,
        record: Record,
        static_override: dict[str, Any] | None = None,
        system_override: str | None = None,
    ) -> EnrichedRecord:
        payload = project(record.payload, self.cfg)
        context = await self.gather_context(record)
        static = {**self.cfg.static_context, **(static_override or {})}
        scope = {
            "record": payload,
            # Skips the include_fields whitelist but redact_fields is still masked —
            # a template must never be able to route around that guarantee.
            "raw": redact(record.payload, self.cfg.redact_fields),
            "context": context,
            "static": static,
            "id": record.id,
            "origin": record.origin,
        }
        try:
            user_prompt = self._template.render(**scope)
            system_prompt = (
                system_override if system_override is not None else self._system.render(**scope)
            )
        except TemplateError as exc:
            raise ValueError(f"prompt template error: {exc}") from exc

        limit = self.cfg.prompt.max_input_chars
        truncated = limit > 0 and len(user_prompt) > limit
        if truncated:
            log.warning("enrich.prompt_truncated", record_id=record.id, length=len(user_prompt))
            user_prompt = user_prompt[:limit] + "\n...[truncated]"

        return EnrichedRecord(
            record=record,
            context=context,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            truncated=truncated,
        )
