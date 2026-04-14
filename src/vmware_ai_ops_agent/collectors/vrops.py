"""
vRealize Operations Manager API collector.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import VROpsConfig
from .models import (
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

logger = structlog.get_logger(__name__)


def _safe_resource_kind(value: str) -> ResourceKind:
    """Safely convert string to ResourceKind, defaulting to VIRTUAL_MACHINE."""
    try:
        return ResourceKind(value)
    except ValueError:
        logger.warning("Unknown resource kind, defaulting to VirtualMachine", kind=value)
        return ResourceKind.VIRTUAL_MACHINE


def _safe_severity(value: str) -> Severity:
    """Safely convert string to Severity, defaulting to WARNING."""
    try:
        return Severity(value)
    except ValueError:
        logger.warning("Unknown severity, defaulting to WARNING", severity=value)
        return Severity.WARNING


def _safe_timestamp(epoch_ms: int | None) -> datetime:
    """Safely convert epoch milliseconds to datetime (always UTC)."""
    if epoch_ms is None or epoch_ms == 0:
        return datetime.utcnow()
    try:
        return datetime.utcfromtimestamp(epoch_ms / 1000)
    except (ValueError, OSError, TypeError):
        return datetime.utcnow()


class VROpsCollector:
    """Collector for vRealize Operations Manager."""

    def __init__(self, config: VROpsConfig):
        self.config = config
        self.base_url = f"https://{config.host}:{config.port}/suite-api/api"
        self._token: str | None = None
        self._token_expires: datetime | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "VROpsCollector":
        self._client = httpx.AsyncClient(
            verify=self.config.verify_ssl,
            timeout=self.config.timeout,
        )
        await self._authenticate()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()

    async def _authenticate(self) -> None:
        """Authenticate with vROps."""
        auth_url = f"{self.base_url}/auth/token/acquire"
        payload = {
            "username": self.config.username,
            "password": self.config.password.get_secret_value(),
        }

        try:
            response = await self._client.post(
                auth_url,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
            self._token = data["token"]
            self._token_expires = datetime.utcnow() + timedelta(hours=5, minutes=30)
            logger.info("vROps authentication successful", host=self.config.host)
        except httpx.HTTPError as e:
            logger.error("vROps authentication failed", error=str(e))
            raise

    async def _ensure_authenticated(self) -> None:
        if not self._token or (self._token_expires and datetime.utcnow() >= self._token_expires):
            await self._authenticate()

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"vRealizeOpsToken {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._ensure_authenticated()
        url = f"{self.base_url}/{endpoint}"
        try:
            response = await self._client.request(
                method, url, params=params, json=json_data, headers=self._get_headers()
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                # Force re-authentication on the next retry attempt
                self._token = None
            raise

    async def _paginate(
        self,
        method: str,
        endpoint: str,
        result_key: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        """Fetch all pages from a paginated vROps endpoint."""
        all_items: list[dict[str, Any]] = []
        page = 0
        base_params: dict[str, Any] = dict(params or {})
        base_params["pageSize"] = page_size

        while True:
            base_params["page"] = page
            data = await self._request(method, endpoint, params=base_params, json_data=json_data)
            items = data.get(result_key, [])
            if not items:
                break
            all_items.extend(items)
            total = data.get("pageInfo", {}).get("totalCount", len(all_items))
            if len(all_items) >= total:
                break
            page += 1

        return all_items

    async def get_resources(
        self,
        resource_kind: ResourceKind | None = None,
        name_filter: str | None = None,
        page_size: int = 1000,
    ) -> list[ResourceIdentifier]:
        params: dict[str, Any] = {}
        if resource_kind:
            params["resourceKind"] = resource_kind.value
        if name_filter:
            params["name"] = name_filter

        items = await self._paginate(
            "GET", "resources", "resourceList", params=params, page_size=page_size
        )
        resources = []
        for item in items:
            resource = ResourceIdentifier(
                id=item["identifier"],
                name=item.get("resourceKey", {}).get("name", "Unknown"),
                kind=_safe_resource_kind(
                    item.get("resourceKey", {}).get("resourceKindKey", "VirtualMachine")
                ),
                adapter_kind=item.get("resourceKey", {}).get("adapterKindKey", "VMWARE"),
            )
            resources.append(resource)
        return resources

    async def get_resource_health(self, resource_id: str) -> ResourceHealth | None:
        try:
            resource_data = await self._request("GET", f"resources/{resource_id}")
            health_data = await self._request(
                "GET",
                f"resources/{resource_id}/stats",
                params={
                    "statKey": [
                        "badge|health",
                        "badge|workload",
                        "badge|anomalies",
                        "badge|faults",
                        "badge|risk",
                    ],
                    "rollUpType": "AVG",
                    "intervalType": "HOURS",
                    "intervalCount": 1,
                },
            )

            resource_key = resource_data.get("resourceKey", {})
            resource = ResourceIdentifier(
                id=resource_id,
                name=resource_key.get("name", "Unknown"),
                kind=_safe_resource_kind(resource_key.get("resourceKindKey", "VirtualMachine")),
                adapter_kind=resource_key.get("adapterKindKey", "VMWARE"),
            )

            health_score = 100.0
            workload_score = 0.0
            anomaly_score = 0.0
            fault_score = 0.0
            risk_score = 0.0

            for stat in health_data.get("values", []):
                stat_key = stat.get("statKey", {}).get("key", "")
                values = stat.get("data", [])
                if values:
                    latest_value = values[-1]
                    if stat_key == "badge|health":
                        health_score = latest_value
                    elif stat_key == "badge|workload":
                        workload_score = latest_value
                    elif stat_key == "badge|anomalies":
                        anomaly_score = latest_value
                    elif stat_key == "badge|faults":
                        fault_score = latest_value
                    elif stat_key == "badge|risk":
                        risk_score = latest_value

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
                workload_score=workload_score,
                anomaly_score=anomaly_score,
                fault_score=fault_score,
                risk_score=risk_score,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def get_alerts(
        self,
        status: str = "ACTIVE",
        criticality: list[Severity] | None = None,
    ) -> list[Alert]:
        params: dict[str, Any] = {"status": status}
        if criticality:
            params["criticality"] = [c.value for c in criticality]

        items = await self._paginate("GET", "alerts", "alerts", params=params)
        alerts = []

        for item in items:
            symptoms = []
            for symptom_data in item.get("alertTriggeredSymptoms", []):
                symptom = Symptom(
                    id=symptom_data.get("symptomDefinitionId", ""),
                    name=symptom_data.get("symptomName", ""),
                    severity=_safe_severity(symptom_data.get("severity", "WARNING")),
                    state=symptom_data.get("state", ""),
                    message=symptom_data.get("message", ""),
                    metric_key=symptom_data.get("metricKey"),
                    triggered_at=_safe_timestamp(symptom_data.get("startTimeUTC")),
                )
                symptoms.append(symptom)

            resource_data = item.get("resource", {}).get("resourceKey", {})
            resource = ResourceIdentifier(
                id=item.get("resource", {}).get("identifier", ""),
                name=resource_data.get("name", "Unknown"),
                kind=_safe_resource_kind(resource_data.get("resourceKindKey", "VirtualMachine")),
            )

            alert = Alert(
                id=item["alertId"],
                alert_definition_id=item.get("alertDefinitionId", ""),
                name=item.get("alertDefinitionName", ""),
                description=item.get("alertDefinitionDescription", ""),
                severity=_safe_severity(item.get("alertCriticality", "WARNING")),
                status=item.get("status", ""),
                resource=resource,
                symptoms=symptoms,
                impact=item.get("impactMessage", ""),
                recommendations=item.get("recommendations", []),
                start_time=_safe_timestamp(item.get("startTimeUTC")),
            )
            alerts.append(alert)

        logger.info("Retrieved alerts", count=len(alerts), status=status)
        return alerts

    async def get_recommendations(self) -> list[Recommendation]:
        items = await self._paginate("GET", "recommendations", "recommendations")
        recommendations = []

        for item in items:
            resource_data = item.get("resource", {}).get("resourceKey", {})
            resource = ResourceIdentifier(
                id=item.get("resource", {}).get("identifier", ""),
                name=resource_data.get("name", "Unknown"),
                kind=_safe_resource_kind(resource_data.get("resourceKindKey", "VirtualMachine")),
            )
            recommendation = Recommendation(
                id=item.get("id", ""),
                description=item.get("description", ""),
                action=item.get("action", ""),
                target_resource=resource,
                reason=item.get("reason", ""),
                savings=item.get("savings", {}),
                confidence=item.get("confidence", 0.0),
                created_at=_safe_timestamp(item.get("createdAt")),
            )
            recommendations.append(recommendation)

        return recommendations

    async def get_anomalies(self, hours: int = 24) -> list[Anomaly]:
        end_time = int(datetime.utcnow().timestamp() * 1000)
        start_time = end_time - (hours * 3600 * 1000)

        data = await self._request(
            "POST",
            "resources/query",
            json_data={
                "resourceKind": ["VirtualMachine", "HostSystem", "Datastore"],
                "statKey": "badge|anomalies",
                "statKeyComparator": "GT",
                "statKeyValue": 25,
                "begin": start_time,
                "end": end_time,
            },
        )

        anomalies = []
        for item in data.get("resourceList", []):
            resource_key = item.get("resourceKey", {})
            resource = ResourceIdentifier(
                id=item["identifier"],
                name=resource_key.get("name", "Unknown"),
                kind=_safe_resource_kind(resource_key.get("resourceKindKey", "VirtualMachine")),
            )
            anomaly_score = float(item.get("statValues", {}).get("badge|anomalies", 0) or 0)
            severity = Severity.CRITICAL if anomaly_score > 75 else Severity.WARNING

            anomaly = Anomaly(
                id=f"vrops-anomaly-{item['identifier']}",
                source="vrops",
                resource=resource,
                anomaly_type="metric_anomaly",
                description=f"Anomaly score {anomaly_score:.1f}% detected on {resource.name}",
                severity=severity,
                confidence=anomaly_score / 100,
                detected_at=datetime.utcnow(),
                context={"anomaly_score": anomaly_score},
            )
            anomalies.append(anomaly)

        return anomalies

    async def _collect_resources_for_kind(
        self, kind: ResourceKind, semaphore: asyncio.Semaphore
    ) -> list[ResourceHealth]:
        """Collect health for all resources of a given kind."""
        resources = await self.get_resources(resource_kind=kind)

        async def get_health_with_limit(res_id: str) -> ResourceHealth | None:
            async with semaphore:
                return await self.get_resource_health(res_id)

        tasks = [get_health_with_limit(res.id) for res in resources]
        health_results = await asyncio.gather(*tasks, return_exceptions=True)

        return [h for h in health_results if isinstance(h, ResourceHealth)]

    async def collect_all(
        self,
        resource_kinds: list[ResourceKind] | None = None,
    ) -> tuple[list[ResourceHealth], list[Alert], list[Recommendation], list[Anomaly]]:
        if resource_kinds is None:
            resource_kinds = [
                ResourceKind.VIRTUAL_MACHINE,
                ResourceKind.HOST_SYSTEM,
                ResourceKind.DATASTORE,
                ResourceKind.CLUSTER,
            ]

        # Shared semaphore across all resource kinds for global rate limiting
        semaphore = asyncio.Semaphore(10)

        # Collect all resource kinds in parallel
        resource_tasks = [
            self._collect_resources_for_kind(kind, semaphore) for kind in resource_kinds
        ]

        # Also fetch alerts, recommendations, anomalies in parallel
        all_results = await asyncio.gather(
            *resource_tasks,
            self.get_alerts(),
            self.get_recommendations(),
            self.get_anomalies(),
        )

        # Unpack results: first N are resource lists, last 3 are alerts/recs/anomalies
        resource_lists = all_results[: len(resource_kinds)]
        alerts, recommendations, anomalies = all_results[-3:]

        # Flatten resource lists
        all_resources = [r for resource_list in resource_lists for r in resource_list]

        logger.info(
            "vROps collection complete",
            resources=len(all_resources),
            alerts=len(alerts),
            recommendations=len(recommendations),
            anomalies=len(anomalies),
        )

        return all_resources, alerts, recommendations, anomalies
