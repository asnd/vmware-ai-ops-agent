"""
Data models for AI analysis results.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Urgency(str, Enum):
    """Action urgency levels."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ActionType(str, Enum):
    """Types of remediation actions."""

    VMOTION = "vmotion"
    STORAGE_VMOTION = "storage_vmotion"
    DRS_REBALANCE = "drs_rebalance"
    SNAPSHOT_CLEANUP = "snapshot_cleanup"
    RESOURCE_RECLAIM = "resource_reclaim"
    HOST_MAINTENANCE = "host_maintenance"
    RESTART_SERVICE = "restart_service"
    NOTIFY = "notify"
    ESCALATE = "escalate"
    INVESTIGATE = "investigate"


class PredictedFailure(BaseModel):
    """Predicted failure from AI analysis."""

    resource_id: str
    resource_name: str
    failure_type: str
    probability: float
    estimated_time_hours: float | None = None
    confidence: float
    indicators: list[str] = Field(default_factory=list)
    historical_matches: list[str] = Field(default_factory=list)


class RemediationStep(BaseModel):
    """Single remediation step."""

    order: int
    action_type: ActionType
    description: str
    target_resource: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    estimated_duration_minutes: int = 5
    rollback_possible: bool = True


class RemediationPlan(BaseModel):
    """Complete remediation plan."""

    id: str
    title: str
    description: str
    urgency: Urgency
    steps: list[RemediationStep] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    expected_outcome: str = ""
    auto_executable: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class RootCauseAnalysis(BaseModel):
    """Root cause analysis result."""

    primary_cause: str
    confidence: float
    contributing_factors: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    similar_incidents: list[str] = Field(default_factory=list)


class CorrelatedEvent(BaseModel):
    """Event correlated across metrics and logs."""

    event_type: str
    source: str
    timestamp: datetime
    description: str
    correlation_score: float


class AnalysisResult(BaseModel):
    """Complete analysis result from AI engine."""

    id: str
    analyzed_at: datetime = Field(default_factory=datetime.utcnow)
    summary: str
    urgency: Urgency
    correlated_events: list[CorrelatedEvent] = Field(default_factory=list)
    predicted_failures: list[PredictedFailure] = Field(default_factory=list)
    root_cause: RootCauseAnalysis | None = None
    remediation_plan: RemediationPlan | None = None
    insights: list[str] = Field(default_factory=list)
    metrics_analyzed: int = 0
    logs_analyzed: int = 0
    alerts_analyzed: int = 0
    model_used: str = ""
    tokens_used: int = 0
    analysis_duration_seconds: float = 0.0

    def requires_immediate_action(self) -> bool:
        return self.urgency in (Urgency.CRITICAL, Urgency.HIGH)

    def has_predictions(self) -> bool:
        return len(self.predicted_failures) > 0

    def get_high_probability_failures(self, threshold: float = 0.7) -> list[PredictedFailure]:
        return [f for f in self.predicted_failures if f.probability >= threshold]
