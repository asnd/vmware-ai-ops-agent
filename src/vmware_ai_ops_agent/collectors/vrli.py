"""
vRealize Log Insight API collector.
"""

import asyncio
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog

from ..config import VRLIConfig
from .models import Anomaly, LogEntry, LogQueryResult, Severity

logger = structlog.get_logger(__name__)

ERROR_PATTERNS = [
    (r"SCSI\s+sense\s+code", "disk_error", Severity.WARNING),
    (r"SCSI\s+device\s+.*\s+not\s+ready", "disk_not_ready", Severity.CRITICAL),
    (r"Lost\s+access\s+to\s+volume", "storage_disconnect", Severity.CRITICAL),
    (r"APD\s+Timeout", "all_paths_down", Severity.CRITICAL),
    (r"PDL\s+detected", "permanent_device_loss", Severity.CRITICAL),
    (r"memory\s+balloon", "memory_pressure", Severity.WARNING),
    (r"Out\s+of\s+memory", "oom", Severity.CRITICAL),
    (r"swap\s+in|swap\s+out", "swapping", Severity.WARNING),
    (r"NIC\s+link\s+is\s+down", "network_link_down", Severity.CRITICAL),
    (r"DVPort.*blocked", "dvport_blocked", Severity.WARNING),
    (r"packet\s+drop", "packet_loss", Severity.WARNING),
    (r"HA\s+failover", "ha_failover", Severity.CRITICAL),
    (r"vMotion\s+failed", "vmotion_failure", Severity.WARNING),
]


class VRLICollector:
    """Collector for vRealize Log Insight."""

    def __init__(self, config: VRLIConfig):
        self.config = config
        self.base_url = f"https://{config.host}:{config.port}/api/v1"
        self._session_id: str | None = None
        self._session_expires: datetime | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "VRLICollector":
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
        auth_url = f"{self.base_url}/sessions"
        payload = {
            "username": self.config.username,
            "password": self.config.password.get_secret_value(),
            "provider": "Local",
        }

        try:
            response = await self._client.post(auth_url, json=payload)
            response.raise_for_status()
            data = response.json()
            self._session_id = data["sessionId"]
            ttl = data.get("ttl", 86400)
            self._session_expires = datetime.utcnow() + timedelta(seconds=ttl - 300)
            logger.info("vRLI authentication successful", host=self.config.host)
        except httpx.HTTPError as e:
            logger.error("vRLI authentication failed", error=str(e))
            raise

    async def _ensure_authenticated(self) -> None:
        is_expired = (
            self._session_expires and datetime.utcnow() >= self._session_expires
        )
        if not self._session_id or is_expired:
            await self._authenticate()

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._session_id}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._ensure_authenticated()
        url = f"{self.base_url}/{endpoint}"
        headers = self._get_headers()
        response = await self._client.request(
            method, url, params=params, json=json_data, headers=headers
        )
        response.raise_for_status()
        return response.json()

    def _get_time_range_params(self, time_range: str) -> dict[str, int]:
        end_time = int(datetime.utcnow().timestamp() * 1000)
        time_deltas = {
            "LAST_5_MINUTES": timedelta(minutes=5),
            "LAST_15_MINUTES": timedelta(minutes=15),
            "LAST_30_MINUTES": timedelta(minutes=30),
            "LAST_1_HOUR": timedelta(hours=1),
            "LAST_6_HOURS": timedelta(hours=6),
            "LAST_12_HOURS": timedelta(hours=12),
            "LAST_24_HOURS": timedelta(hours=24),
        }
        delta = time_deltas.get(time_range, timedelta(hours=1))
        start_time = end_time - int(delta.total_seconds() * 1000)
        return {"startTimeMillis": start_time, "endTimeMillis": end_time}

    async def query_logs(
        self,
        query: str,
        time_range: str | None = None,
        limit: int | None = None,
        order: str = "DESC",
    ) -> LogQueryResult:
        if time_range is None:
            time_range = self.config.query.get("default_time_range", "LAST_1_HOUR")
        if limit is None:
            limit = self.config.query.get("max_results", 10000)

        time_params = self._get_time_range_params(time_range)
        payload = {
            "logQuery": query,
            "startTimeMillis": time_params["startTimeMillis"],
            "endTimeMillis": time_params["endTimeMillis"],
            "limit": limit,
            "order": order,
        }

        data = await self._request("POST", "events/query", json_data=payload)

        entries = []
        for event in data.get("results", []):
            entry = LogEntry(
                id=event.get("id", ""),
                timestamp=datetime.fromtimestamp(event.get("timestamp", 0) / 1000),
                source=event.get("source", ""),
                source_type=event.get("sourceType", ""),
                facility=event.get("facility", ""),
                severity=event.get("severity", ""),
                app_name=event.get("appName", ""),
                text=event.get("text", ""),
                fields=event.get("fields", {}),
            )
            entries.append(entry)

        return LogQueryResult(
            total_count=data.get("totalCount", 0),
            returned_count=len(entries),
            query=query,
            time_range=time_range,
            entries=entries,
        )

    async def query_error_logs(
        self, time_range: str = "LAST_1_HOUR", limit: int = 1000
    ) -> LogQueryResult:
        query = "error OR warning OR critical OR emergency OR alert"
        return await self.query_logs(query, time_range, limit)

    async def detect_anomalies(self, time_range: str = "LAST_1_HOUR") -> list[Anomaly]:
        anomalies = []
        recent_result = await self.query_error_logs(time_range, limit=5000)

        pattern_counts: Counter = Counter()

        for entry in recent_result.entries:
            for pattern, pattern_name, severity in ERROR_PATTERNS:
                if re.search(pattern, entry.text, re.IGNORECASE):
                    pattern_counts[pattern_name] += 1

                    if severity == Severity.CRITICAL:
                        anomaly = Anomaly(
                            id=f"vrli-pattern-{entry.id}",
                            source="vrli",
                            resource=None,
                            anomaly_type="log_pattern",
                            description=(
                                f"Critical pattern '{pattern_name}' detected: "
                                f"{entry.text[:200]}"
                            ),
                            severity=severity,
                            confidence=0.9,
                            detected_at=entry.timestamp,
                            related_logs=[entry.id],
                            context={"pattern": pattern_name, "source": entry.source},
                        )
                        anomalies.append(anomaly)

        for pattern_name, count in pattern_counts.items():
            if count >= 10:
                severity_map = {name: sev for _, name, sev in ERROR_PATTERNS}
                severity = severity_map.get(pattern_name, Severity.WARNING)

                anomaly = Anomaly(
                    id=f"vrli-frequency-{pattern_name}-{int(datetime.utcnow().timestamp())}",
                    source="vrli",
                    resource=None,
                    anomaly_type="log_frequency",
                    description=f"High frequency of '{pattern_name}' pattern: {count} occurrences",
                    severity=severity,
                    confidence=min(count / 50, 1.0),
                    detected_at=datetime.utcnow(),
                    context={"pattern": pattern_name, "count": count},
                )
                anomalies.append(anomaly)

        logger.info("Log anomaly detection complete", anomalies=len(anomalies))
        return anomalies

    async def collect_all(
        self, time_range: str = "LAST_1_HOUR"
    ) -> tuple[list[LogEntry], list[Anomaly]]:
        error_result, anomalies = await asyncio.gather(
            self.query_error_logs(time_range), self.detect_anomalies(time_range)
        )

        logger.info(
            "vRLI collection complete",
            logs=len(error_result.entries),
            anomalies=len(anomalies),
        )
        return error_result.entries, anomalies
