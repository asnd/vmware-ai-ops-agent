"""
MCP client for AriaOps (VMware Aria Operations) via ariaops_mcp server.

Replaces the direct VROpsCollector with MCP protocol-based communication.
Inherits session lifecycle, SSE response handling, and transport-level retry
from BaseMCPClient.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import structlog

from ..collectors.models import (
    Alert,
    Anomaly,
    HealthState,
    Recommendation,
    ResourceHealth,
    ResourceIdentifier,
    ResourceKind,
    Severity,
    Symptom,
)
from .base import BaseMCPClient

logger = structlog.get_logger(__name__)


def _safe_resource_kind(value: str) -> ResourceKind:
    try:
        return ResourceKind(value)
    except ValueError:
        return ResourceKind.VIRTUAL_MACHINE


def _safe_severity(value: str) -> Severity:
    mapping = {
        "CRITICAL": Severity.CRITICAL,
        "IMMEDIATE": Severity.IMMEDIATE,
        "WARNING": Severity.WARNING,
        "INFO": Severity.INFO,
        "INFORMATION": Severity.INFO,
    }
    return mapping.get(value.upper(), Severity.WARNING)


def _safe_timestamp(epoch_ms: int | None) -> datetime:
    if epoch_ms is None or epoch_ms == 0:
        return datetime.utcnow()
    try:
        return datetime.utcfromtimestamp(epoch_ms / 1000)
    except (ValueError, OSError, TypeError):
        return datetime.utcnow()


class AriaOpsMCPClient(BaseMCPClient):
    """MCP client adapter for ariaops_mcp server.

    Communicates via MCP Streamable HTTP transport. Inherits session
    lifecycle, SSE/JSON response parsing, and retry from BaseMCPClient.
    """

    def __init__(
        self,
        base_url: str,
        auth_token: str | None = None,
        timeout: float = 120.0,
    ):
        super().__init__(base_url=base_url, auth_token=auth_token, timeout=timeout)

    # --- Resource Tools ---

    async def list_resources(
        self,
        resource_kind: str | None = None,
        adapter_kind: str | None = None,
        name: str | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """List resources from Aria Operations."""
        args: dict[str, Any] = {"page_size": page_size}
        if resource_kind:
            args["resource_kind"] = resource_kind
        if adapter_kind:
            args["adapter_kind"] = adapter_kind
        if name:
            args["name"] = name
        result = await self._call_tool("list_resources", args)
        return result.get("resources", []) if isinstance(result, dict) else []

    async def get_resource(self, resource_id: str) -> dict[str, Any]:
        """Get a single resource by ID."""
        return await self._call_tool("get_resource", {"resource_id": resource_id})

    async def get_resource_properties(self, resource_id: str) -> dict[str, Any]:
        """Get properties for a resource."""
        return await self._call_tool("get_resource_properties", {"resource_id": resource_id})

    # --- Alert Tools ---

    async def list_alerts(
        self,
        status: str = "ACTIVE",
        criticality: str | None = None,
        resource_id: str | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """List alerts from Aria Operations."""
        args: dict[str, Any] = {"status": status, "page_size": page_size}
        if criticality:
            args["criticality"] = criticality
        if resource_id:
            args["resource_id"] = resource_id
        result = await self._call_tool("list_alerts", args)
        return result.get("alerts", []) if isinstance(result, dict) else []

    async def get_alert(self, alert_id: str) -> dict[str, Any]:
        """Get a single alert by ID."""
        return await self._call_tool("get_alert", {"alert_id": alert_id})

    # --- Metrics Tools ---

    async def get_resource_stats(
        self,
        resource_id: str,
        stat_keys: list[str],
        rollup_type: str = "AVG",
        interval_type: str = "HOURS",
        interval_count: int = 1,
        begin: int | None = None,
        end: int | None = None,
    ) -> dict[str, Any]:
        """Get historical stats for a resource."""
        args: dict[str, Any] = {
            "resource_id": resource_id,
            "stat_keys": stat_keys,
            "rollup_type": rollup_type,
            "interval_type": interval_type,
            "interval_count": interval_count,
        }
        if begin is not None:
            args["begin"] = begin
        if end is not None:
            args["end"] = end
        return await self._call_tool("get_resource_stats", args)

    async def get_latest_stats(
        self,
        resource_id: str,
        stat_keys: list[str],
    ) -> dict[str, Any]:
        """Get latest stat values for a resource."""
        return await self._call_tool(
            "get_latest_stats",
            {"resource_id": resource_id, "stat_keys": stat_keys},
        )

    # --- Capacity Tools ---

    async def get_capacity_remaining(self, resource_id: str) -> dict[str, Any]:
        """Get capacity remaining for a resource."""
        return await self._call_tool("get_capacity_remaining", {"resource_id": resource_id})

    async def get_capacity_forecast(
        self,
        resource_id: str,
        stat_key: str,
        days_ahead: int = 30,
    ) -> dict[str, Any]:
        """Get capacity forecast for a resource."""
        return await self._call_tool(
            "get_capacity_forecast",
            {"resource_id": resource_id, "stat_key": stat_key, "days_ahead": days_ahead},
        )

    async def get_trend_analysis(
        self,
        resource_id: str,
        stat_key: str,
        interval_type: str = "DAYS",
        interval_count: int = 7,
    ) -> dict[str, Any]:
        """Get trend analysis for a resource metric."""
        return await self._call_tool(
            "get_trend_analysis",
            {
                "resource_id": resource_id,
                "stat_key": stat_key,
                "interval_type": interval_type,
                "interval_count": interval_count,
            },
        )

    # --- Write Operations (opt-in) ---

    async def modify_alerts(
        self,
        alert_ids: list[str],
        status: str,
    ) -> dict[str, Any]:
        """Modify alert status (cancel/suspend)."""
        return await self._call_tool(
            "modify_alerts",
            {"alert_ids": alert_ids, "status": status},
        )

    async def mark_resources_maintained(
        self,
        resource_ids: list[str],
        duration_minutes: int = 60,
    ) -> dict[str, Any]:
        """Put resources into maintenance mode."""
        return await self._call_tool(
            "mark_resources_maintained",
            {"resource_ids": resource_ids, "duration_minutes": duration_minutes},
        )

    async def unmark_resources_maintained(
        self,
        resource_ids: list[str],
    ) -> dict[str, Any]:
        """Remove resources from maintenance mode."""
        return await self._call_tool(
            "unmark_resources_maintained",
            {"resource_ids": resource_ids},
        )

    # --- High-Level Collection (replaces VROpsCollector.collect_all) ---

    async def collect_all(
        self,
        resource_kinds: list[str] | None = None,
    ) -> tuple[list[ResourceHealth], list[Alert], list[Recommendation], list[Anomaly]]:
        """Collect full infrastructure state via MCP tools."""
        if resource_kinds is None:
            resource_kinds = ["VirtualMachine", "HostSystem", "Datastore", "ClusterComputeResource"]

        resource_tasks = [self.list_resources(resource_kind=kind) for kind in resource_kinds]
        alerts_task = self.list_alerts(status="ACTIVE")

        all_results = await asyncio.gather(*resource_tasks, alerts_task, return_exceptions=True)

        resource_lists = all_results[: len(resource_kinds)]
        alerts_raw = all_results[-1] if not isinstance(all_results[-1], Exception) else []

        all_resources: list[ResourceHealth] = []
        for raw_list in resource_lists:
            if isinstance(raw_list, Exception):
                logger.error("Resource collection failed", error=str(raw_list))
                continue
            for item in raw_list:
                resource = self._parse_resource_health(item)
                if resource:
                    all_resources.append(resource)

        alerts: list[Alert] = []
        if isinstance(alerts_raw, list):
            for item in alerts_raw:
                alert = self._parse_alert(item)
                if alert:
                    alerts.append(alert)

        logger.info(
            "AriaOps MCP collection complete",
            resources=len(all_resources),
            alerts=len(alerts),
        )
        return all_resources, alerts, [], []

    def _parse_resource_health(self, data: dict[str, Any]) -> ResourceHealth | None:
        try:
            resource_key = data.get("resourceKey", data)
            resource = ResourceIdentifier(
                id=data.get("identifier", data.get("id", "")),
                name=resource_key.get("name", data.get("name", "Unknown")),
                kind=_safe_resource_kind(
                    resource_key.get("resourceKindKey", data.get("resourceKind", "VirtualMachine"))
                ),
                adapter_kind=resource_key.get("adapterKindKey", data.get("adapterKind", "VMWARE")),
            )

            health_score = float(data.get("health", data.get("health_score", 75.0)))
            if health_score >= 75:
                health_state = HealthState.GREEN
            elif health_score >= 50:
                health_state = HealthState.YELLOW
            elif health_score >= 25:
                health_state = HealthState.ORANGE
            else:
                health_state = HealthState.RED

            return ResourceHealth(
                resource=resource,
                health_state=health_state,
                health_score=health_score,
            )
        except Exception as e:
            logger.warning("Failed to parse resource", error=str(e), data=str(data)[:200])
            return None

    def _parse_alert(self, data: dict[str, Any]) -> Alert | None:
        try:
            resource_data = data.get("resource", {})
            resource_key = resource_data.get("resourceKey", resource_data)
            resource = ResourceIdentifier(
                id=resource_data.get("identifier", resource_data.get("id", "")),
                name=resource_key.get("name", resource_data.get("name", "Unknown")),
                kind=_safe_resource_kind(
                    resource_key.get(
                        "resourceKindKey",
                        resource_data.get("resourceKind", "VirtualMachine"),
                    )
                ),
            )

            symptoms = []
            for s in data.get("symptoms", data.get("alertTriggeredSymptoms", [])):
                symptoms.append(
                    Symptom(
                        id=s.get("symptomDefinitionId", s.get("id", "")),
                        name=s.get("symptomName", s.get("name", "")),
                        severity=_safe_severity(s.get("severity", "WARNING")),
                        state=s.get("state", ""),
                        message=s.get("message", ""),
                        metric_key=s.get("metricKey"),
                        triggered_at=_safe_timestamp(s.get("startTimeUTC")),
                    )
                )

            criticality = data.get("alertCriticality", data.get("criticality", "WARNING"))
            return Alert(
                id=data.get("alertId", data.get("id", "")),
                alert_definition_id=data.get("alertDefinitionId", ""),
                name=data.get("alertDefinitionName", data.get("name", "")),
                description=data.get("alertDefinitionDescription", data.get("description", "")),
                severity=_safe_severity(criticality),
                status=data.get("status", "ACTIVE"),
                resource=resource,
                symptoms=symptoms,
                impact=data.get("impactMessage", data.get("impact", "")),
                recommendations=data.get("recommendations", []),
                start_time=_safe_timestamp(data.get("startTimeUTC", data.get("startTime"))),
            )
        except Exception as e:
            logger.warning("Failed to parse alert", error=str(e), data=str(data)[:200])
            return None
