"""
Data models for VMware infrastructure data.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """Alert severity levels."""

    CRITICAL = "CRITICAL"
    IMMEDIATE = "IMMEDIATE"
    WARNING = "WARNING"
    INFO = "INFO"


class ResourceKind(str, Enum):
    """VMware resource types."""

    VIRTUAL_MACHINE = "VirtualMachine"
    HOST_SYSTEM = "HostSystem"
    CLUSTER = "ClusterComputeResource"
    DATASTORE = "Datastore"
    DISTRIBUTED_VIRTUAL_SWITCH = "VmwareDistributedVirtualSwitch"
    DISTRIBUTED_VIRTUAL_PORTGROUP = "DistributedVirtualPortgroup"
    RESOURCE_POOL = "ResourcePool"
    DATACENTER = "Datacenter"
    VCENTER = "VMwareAdapter Instance"


class HealthState(str, Enum):
    """Resource health states."""

    GREEN = "GREEN"
    YELLOW = "YELLOW"
    ORANGE = "ORANGE"
    RED = "RED"
    GREY = "GREY"


class ResourceIdentifier(BaseModel):
    """Resource identifier from vROps."""

    id: str
    name: str
    kind: ResourceKind
    adapter_kind: str = "VMWARE"
    parent_id: str | None = None


class Metric(BaseModel):
    """Time-series metric from vROps."""

    resource_id: str
    resource_name: str
    stat_key: str
    timestamps: list[int] = Field(default_factory=list)
    values: list[float] = Field(default_factory=list)
    unit: str = ""

    @property
    def latest_value(self) -> float | None:
        """Get the most recent metric value."""
        return self.values[-1] if self.values else None

    @property
    def average(self) -> float | None:
        """Calculate average of all values."""
        return sum(self.values) / len(self.values) if self.values else None

    @property
    def max_value(self) -> float | None:
        """Get the maximum value."""
        return max(self.values) if self.values else None


class ResourceHealth(BaseModel):
    """Resource health information from vROps."""

    resource: ResourceIdentifier
    health_state: HealthState
    health_score: float = 0.0
    workload_score: float = 0.0
    anomaly_score: float = 0.0
    fault_score: float = 0.0
    risk_score: float = 0.0
    time_remaining: float | None = None
    metrics: dict[str, Metric] = Field(default_factory=dict)
    collected_at: datetime = Field(default_factory=datetime.utcnow)

    def is_critical(self) -> bool:
        """Check if resource is in critical state."""
        return self.health_state == HealthState.RED or self.health_score < 25

    def is_warning(self) -> bool:
        """Check if resource is in warning state."""
        is_unhealthy = self.health_state in (HealthState.YELLOW, HealthState.ORANGE)
        return is_unhealthy or self.health_score < 50


class Symptom(BaseModel):
    """Alert symptom from vROps."""

    id: str
    name: str
    severity: Severity
    state: str
    message: str
    metric_key: str | None = None
    triggered_at: datetime


class Alert(BaseModel):
    """Alert from vROps."""

    id: str
    alert_definition_id: str
    name: str
    description: str
    severity: Severity
    status: str
    resource: ResourceIdentifier
    symptoms: list[Symptom] = Field(default_factory=list)
    impact: str = ""
    recommendations: list[str] = Field(default_factory=list)
    start_time: datetime
    update_time: datetime | None = None
    cancel_time: datetime | None = None

    def is_active(self) -> bool:
        """Check if alert is currently active."""
        return self.status == "ACTIVE"


class Recommendation(BaseModel):
    """Action recommendation from vROps."""

    id: str
    description: str
    action: str
    target_resource: ResourceIdentifier
    reason: str
    savings: dict[str, float] = Field(default_factory=dict)
    confidence: float = 0.0
    created_at: datetime


class LogEntry(BaseModel):
    """Log entry from vRLI."""

    id: str
    timestamp: datetime
    source: str
    source_type: str
    facility: str = ""
    severity: str = ""
    app_name: str = ""
    text: str
    fields: dict[str, Any] = Field(default_factory=dict)

    def contains_error(self) -> bool:
        """Check if log indicates an error."""
        error_keywords = ["error", "fail", "critical", "emergency", "alert"]
        return any(kw in self.text.lower() for kw in error_keywords)


class LogQueryResult(BaseModel):
    """Result of a vRLI log query."""

    total_count: int
    returned_count: int
    query: str
    time_range: str
    entries: list[LogEntry] = Field(default_factory=list)


class Anomaly(BaseModel):
    """Anomaly detected in metrics or logs."""

    id: str
    source: str
    resource: ResourceIdentifier | None = None
    anomaly_type: str
    description: str
    severity: Severity
    confidence: float
    detected_at: datetime
    related_metrics: list[str] = Field(default_factory=list)
    related_logs: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class InfrastructureState(BaseModel):
    """Complete infrastructure state snapshot."""

    collected_at: datetime = Field(default_factory=datetime.utcnow)
    resources: list[ResourceHealth] = Field(default_factory=list)
    alerts: list[Alert] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    anomalies: list[Anomaly] = Field(default_factory=list)
    recent_logs: list[LogEntry] = Field(default_factory=list)

    @property
    def critical_alerts(self) -> list[Alert]:
        """Get all critical active alerts."""
        return [a for a in self.alerts if a.is_active() and a.severity == Severity.CRITICAL]

    @property
    def unhealthy_resources(self) -> list[ResourceHealth]:
        """Get all resources in warning or critical state."""
        return [r for r in self.resources if r.is_warning() or r.is_critical()]

    def get_resources_by_kind(self, kind: ResourceKind) -> list[ResourceHealth]:
        """Get resources of a specific type."""
        return [r for r in self.resources if r.resource.kind == kind]

    def get_alerts_for_resource(self, resource_id: str) -> list[Alert]:
        """Get alerts for a specific resource."""
        return [a for a in self.alerts if a.resource.id == resource_id]
