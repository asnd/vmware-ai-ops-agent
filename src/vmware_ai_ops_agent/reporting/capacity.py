"""
Capacity / time-to-exhaustion reporting.

Builds a forward-looking report by reusing the AriaOps MCP capacity tools
(``list_resources`` + ``get_capacity_remaining``). Read-only; no remediation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from ..mcp_clients.ariaops import AriaOpsMCPClient

logger = structlog.get_logger(__name__)


@dataclass
class CapacityEntry:
    """Capacity standing for a single resource."""

    resource_id: str
    resource_name: str
    remaining_percent: float | None
    time_remaining_days: float | None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resource_name(raw: dict[str, Any]) -> str:
    resource_key = raw.get("resourceKey", raw)
    if isinstance(resource_key, dict):
        return resource_key.get("name", raw.get("name", "Unknown"))
    return raw.get("name", "Unknown")


def _parse_capacity(resource_id: str, name: str, data: dict[str, Any]) -> CapacityEntry:
    # Aria Operations responses vary in casing/keys depending on server version;
    # accept the common variants (mirrors graph.py's enrichment parsing).
    remaining = data.get(
        "remaining_capacity",
        data.get("remainingCapacity", data.get("remaining_percent")),
    )
    time_remaining = data.get(
        "time_remaining",
        data.get("timeRemaining", data.get("time_remaining_days")),
    )
    return CapacityEntry(
        resource_id=resource_id,
        resource_name=name,
        remaining_percent=_to_float(remaining),
        time_remaining_days=_to_float(time_remaining),
    )


async def build_capacity_report(
    client: AriaOpsMCPClient,
    resource_kind: str = "HostSystem",
    limit: int = 20,
) -> list[CapacityEntry]:
    """Return capacity entries for ``resource_kind``, soonest-exhaustion first.

    Resources whose ``time_remaining_days`` is unknown sort last. A failed
    per-resource lookup degrades to an entry with empty fields rather than
    failing the whole report.
    """
    resources = await client.list_resources(resource_kind=resource_kind, page_size=limit)

    entries: list[CapacityEntry] = []
    for raw in resources[:limit]:
        resource_id = raw.get("identifier", raw.get("id", ""))
        if not resource_id:
            continue
        name = _resource_name(raw)
        try:
            data = await client.get_capacity_remaining(resource_id)
        except Exception as e:
            logger.warning("Capacity lookup failed", resource_id=resource_id, error=str(e))
            data = {}
        if not isinstance(data, dict):
            data = {}
        entries.append(_parse_capacity(resource_id, name, data))

    entries.sort(
        key=lambda e: (
            e.time_remaining_days is None,
            e.time_remaining_days if e.time_remaining_days is not None else 0.0,
        )
    )

    logger.info("Capacity report built", resource_kind=resource_kind, count=len(entries))
    return entries
