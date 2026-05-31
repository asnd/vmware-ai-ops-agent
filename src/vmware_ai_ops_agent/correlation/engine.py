"""
Correlation engine for VMware infrastructure analysis.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from ..collectors.models import (
    Alert,
    Anomaly,
    InfrastructureState,
    LogEntry,
    ResourceHealth,
    Severity,
)
from .patterns import KnownPattern, PatternMatcher

logger = structlog.get_logger(__name__)


@dataclass
class CorrelatedIssue:
    """An issue correlated across multiple data sources."""

    id: str
    pattern: KnownPattern | None
    severity: Severity
    description: str
    confidence: float
    resources: list[ResourceHealth] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    logs: list[LogEntry] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    first_detected: datetime = field(default_factory=datetime.utcnow)
    last_updated: datetime = field(default_factory=datetime.utcnow)
    root_cause_hypothesis: str = ""
    predicted_impact: str = ""
    recommended_actions: list[str] = field(default_factory=list)

    def source_count(self) -> int:
        count = 0
        if self.resources:
            count += 1
        if self.alerts:
            count += 1
        if self.logs:
            count += 1
        if self.anomalies:
            count += 1
        return count


@dataclass
class CorrelationResult:
    """Result of correlation analysis."""

    timestamp: datetime = field(default_factory=datetime.utcnow)
    issues: list[CorrelatedIssue] = field(default_factory=list)
    patterns_matched: int = 0
    resources_analyzed: int = 0
    logs_analyzed: int = 0
    alerts_analyzed: int = 0

    @property
    def critical_issues(self) -> list[CorrelatedIssue]:
        return [i for i in self.issues if i.severity == Severity.CRITICAL]

    @property
    def high_confidence_issues(self) -> list[CorrelatedIssue]:
        return [i for i in self.issues if i.confidence >= 0.7]


class CorrelationEngine:
    """Engine for correlating infrastructure data."""

    def __init__(self, patterns: list[KnownPattern] | None = None):
        # ``patterns=None`` uses the built-in KNOWN_PATTERNS; callers can pass a
        # merged list (built-ins + site-specific) loaded from config.
        self.pattern_matcher = PatternMatcher(patterns)
        self._issue_counter = 0

    def _generate_issue_id(self) -> str:
        self._issue_counter += 1
        return f"issue-{self._issue_counter}-{int(datetime.utcnow().timestamp())}"

    def correlate(self, state: InfrastructureState) -> CorrelationResult:
        result = CorrelationResult(
            resources_analyzed=len(state.resources),
            logs_analyzed=len(state.recent_logs),
            alerts_analyzed=len(state.alerts),
        )

        log_matches = self.pattern_matcher.match_logs(state.recent_logs)
        metric_matches = self.pattern_matcher.match_metrics(state.resources)
        alert_matches = self.pattern_matcher.match_alerts(state.alerts)

        result.patterns_matched = len(log_matches) + len(metric_matches) + len(alert_matches)

        pattern_evidence: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"logs": [], "resources": [], "alerts": []}
        )

        for pattern, logs in log_matches:
            pattern_evidence[pattern.id]["logs"].extend(logs)
            pattern_evidence[pattern.id]["pattern"] = pattern

        for pattern, resources in metric_matches:
            pattern_evidence[pattern.id]["resources"].extend(resources)
            pattern_evidence[pattern.id]["pattern"] = pattern

        for pattern, alerts in alert_matches:
            pattern_evidence[pattern.id]["alerts"].extend(alerts)
            pattern_evidence[pattern.id]["pattern"] = pattern

        for _pattern_id, evidence in pattern_evidence.items():
            pattern = evidence.get("pattern")
            if not pattern:
                continue

            source_count = sum(1 for k in ["logs", "resources", "alerts"] if evidence[k])
            base_confidence = min(source_count * 0.3 + 0.2, 0.95)

            issue = CorrelatedIssue(
                id=self._generate_issue_id(),
                pattern=pattern,
                severity=pattern.severity,
                description=f"{pattern.name}: {pattern.description}",
                confidence=base_confidence,
                resources=evidence["resources"],
                alerts=evidence["alerts"],
                logs=evidence["logs"],
                root_cause_hypothesis=pattern.predicted_failure,
                predicted_impact=pattern.predicted_failure,
                recommended_actions=pattern.recommended_actions,
            )

            all_times = []
            for log in evidence["logs"]:
                all_times.append(log.timestamp)
            for alert in evidence["alerts"]:
                all_times.append(alert.start_time)

            if all_times:
                issue.first_detected = min(all_times)
                issue.last_updated = max(all_times)

            result.issues.append(issue)

        self._correlate_anomalies(state.anomalies, result.issues)
        self._correlate_unhealthy_resources(state, result)

        result.issues.sort(
            key=lambda i: (
                {"CRITICAL": 0, "IMMEDIATE": 1, "WARNING": 2, "INFO": 3}.get(i.severity.value, 4),
                -i.confidence,
            )
        )

        logger.info(
            "Correlation complete",
            issues=len(result.issues),
            critical=len(result.critical_issues),
            patterns_matched=result.patterns_matched,
        )

        return result

    def _correlate_anomalies(self, anomalies: list[Anomaly], issues: list[CorrelatedIssue]) -> None:
        for anomaly in anomalies:
            matched = False
            for issue in issues:
                if anomaly.resource:
                    for resource in issue.resources:
                        if resource.resource.id == anomaly.resource.id:
                            issue.anomalies.append(anomaly)
                            issue.confidence = min(issue.confidence + 0.1, 0.99)
                            matched = True
                            break

            if not matched and anomaly.severity == Severity.CRITICAL:
                new_issue = CorrelatedIssue(
                    id=self._generate_issue_id(),
                    pattern=None,
                    severity=anomaly.severity,
                    description=anomaly.description,
                    confidence=anomaly.confidence,
                    anomalies=[anomaly],
                    first_detected=anomaly.detected_at,
                )
                issues.append(new_issue)

    def _correlate_unhealthy_resources(
        self, state: InfrastructureState, result: CorrelationResult
    ) -> None:
        matched_resource_ids = set()
        for issue in result.issues:
            for resource in issue.resources:
                matched_resource_ids.add(resource.resource.id)

        for resource in state.resources:
            if resource.resource.id in matched_resource_ids:
                continue

            if resource.is_critical():
                issue = CorrelatedIssue(
                    id=self._generate_issue_id(),
                    pattern=None,
                    severity=Severity.CRITICAL,
                    description=f"Critical health state on {resource.resource.name}",
                    confidence=0.8,
                    resources=[resource],
                    root_cause_hypothesis=f"Health score: {resource.health_score:.1f}%",
                )
                issue.alerts = state.get_alerts_for_resource(resource.resource.id)
                result.issues.append(issue)
