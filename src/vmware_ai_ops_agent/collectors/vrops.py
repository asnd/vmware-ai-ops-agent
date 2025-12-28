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
        response = await self._client.request(
            method, url, params=params, json=json_data, headers=self._get_headers()
        )
        response.raise_for_status()
        return response.json()

    async def get_resources(
        self,
        resource_kind: ResourceKind | None = None,
        name_filter: str | None = None,
        page_size: int = 1000,
    ) -> list[ResourceIdentifier]:
        params = {"pageSize": page_size}
        if resource_kind:
            params["resourceKind"] = resource_kind.value
        if name_filter:
            params["name"] = name_filter

        data = await self._request("GET", "resources", params=params)
        resources = []
        for item in data.get("resourceList", []):
            resource = ResourceIdentifier(
                id=item["identifier"],
                name=item.get("resourceKey", {}).get("name", "Unknown"),
                kind=ResourceKind(
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
                kind=ResourceKind(resource_key.get("resourceKindKey", "VirtualMachine")),
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

        data = await self._request("GET", "alerts", params=params)
        alerts = []

        for item in data.get("alerts", []):
            symptoms = []
            for symptom_data in item.get("alertTriggeredSymptoms", []):
                symptom = Symptom(
                    id=symptom_data.get("symptomDefinitionId", ""),
                    name=symptom_data.get("symptomName", ""),
                    severity=Severity(symptom_data.get("severity", "WARNING")),
                    state=symptom_data.get("state", ""),
                    message=symptom_data.get("message", ""),
                    metric_key=symptom_data.get("metricKey"),
                    triggered_at=datetime.fromtimestamp(symptom_data.get("startTimeUTC", 0) / 1000),
                )
                symptoms.append(symptom)

            resource_data = item.get("resource", {}).get("resourceKey", {})
            resource = ResourceIdentifier(
                id=item.get("resource", {}).get("identifier", ""),
                name=resource_data.get("name", "Unknown"),
                kind=ResourceKind(resource_data.get("resourceKindKey", "VirtualMachine")),
            )

            alert = Alert(
                id=item["alertId"],
                alert_definition_id=item.get("alertDefinitionId", ""),
                name=item.get("alertDefinitionName", ""),
                description=item.get("alertDefinitionDescription", ""),
                severity=Severity(item.get("alertCriticality", "WARNING")),
                status=item.get("status", ""),
                resource=resource,
                symptoms=symptoms,
                impact=item.get("impactMessage", ""),
                recommendations=item.get("recommendations", []),
                start_time=datetime.fromtimestamp(item.get("startTimeUTC", 0) / 1000),
            )
            alerts.append(alert)

        logger.info("Retrieved alerts", count=len(alerts), status=status)
        return alerts

    async def get_recommendations(self) -> list[Recommendation]:
        data = await self._request("GET", "recommendations")
        recommendations = []

        for item in data.get("recommendations", []):
            resource_data = item.get("resource", {}).get("resourceKey", {})
            resource = ResourceIdentifier(
                id=item.get("resource", {}).get("identifier", ""),
                name=resource_data.get("name", "Unknown"),
                kind=ResourceKind(resource_data.get("resourceKindKey", "VirtualMachine")),
            )
            recommendation = Recommendation(
                id=item.get("id", ""),
                description=item.get("description", ""),
                action=item.get("action", ""),
                target_resource=resource,
                reason=item.get("reason", ""),
                savings=item.get("savings", {}),
                confidence=item.get("confidence", 0.0),
                created_at=datetime.fromtimestamp(item.get("createdAt", 0) / 1000),
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
                kind=ResourceKind(resource_key.get("resourceKindKey", "VirtualMachine")),
            )
            anomaly_score = item.get("statValues", {}).get("badge|anomalies", 0)
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

        all_resources = []
        for kind in resource_kinds:
            resources = await self.get_resources(resource_kind=kind)
            semaphore = asyncio.Semaphore(10)

            async def get_health_with_limit(res_id: str, sem: asyncio.Semaphore):
                async with sem:
                    return await self.get_resource_health(res_id)

            tasks = [
                get_health_with_limit(res["identifier"], semaphore)
                for res in resources
            ]
            health_results = await asyncio.gather(*tasks, return_exceptions=True)

            for health in health_results:
                if isinstance(health, ResourceHealth):
                    all_resources.append(health)

        alerts, recommendations, anomalies = await asyncio.gather(
            self.get_alerts(),
            self.get_recommendations(),
            self.get_anomalies(),
        )

        logger.info(
            "vROps collection complete",
            resources=len(all_resources),
            alerts=len(alerts),
            recommendations=len(recommendations),
            anomalies=len(anomalies),
        )

        return all_resources, alerts, recommendations, anomalies
