"""
Pattern definitions for infrastructure issue detection.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import structlog
import yaml

from ..collectors.models import Alert, LogEntry, ResourceHealth, Severity

logger = structlog.get_logger(__name__)


class PatternCategory(str, Enum):
    STORAGE = "storage"
    NETWORK = "network"
    COMPUTE = "compute"
    MEMORY = "memory"
    AVAILABILITY = "availability"


@dataclass
class KnownPattern:
    """Definition of a known issue pattern."""

    id: str
    name: str
    category: PatternCategory
    description: str
    severity: Severity
    log_patterns: list[str] = field(default_factory=list)
    metric_conditions: dict[str, tuple[str, float]] = field(default_factory=dict)
    alert_names: list[str] = field(default_factory=list)
    predicted_failure: str = ""
    failure_probability: float = 0.5
    recommended_actions: list[str] = field(default_factory=list)
    auto_remediate: bool = False


KNOWN_PATTERNS: list[KnownPattern] = [
    KnownPattern(
        id="storage-apd",
        name="All Paths Down (APD)",
        category=PatternCategory.STORAGE,
        description="Storage connectivity lost on all paths",
        severity=Severity.CRITICAL,
        log_patterns=[r"APD\s+Timeout", r"Lost\s+access\s+to\s+volume", r"All\s+paths\s+down"],
        alert_names=["Datastore connectivity lost", "APD detected"],
        predicted_failure="Complete storage outage",
        failure_probability=0.95,
        recommended_actions=["Check storage array connectivity", "Verify HBA status"],
    ),
    KnownPattern(
        id="storage-pdl",
        name="Permanent Device Loss (PDL)",
        category=PatternCategory.STORAGE,
        description="Storage device permanently unavailable",
        severity=Severity.CRITICAL,
        log_patterns=[r"PDL\s+detected", r"Permanent\s+device\s+loss"],
        alert_names=["PDL detected"],
        predicted_failure="Data loss if not addressed",
        failure_probability=0.99,
        recommended_actions=["Do NOT access affected datastore", "Contact storage team"],
    ),
    KnownPattern(
        id="storage-latency-high",
        name="High Storage Latency",
        category=PatternCategory.STORAGE,
        description="Storage response times above threshold",
        severity=Severity.WARNING,
        log_patterns=[r"SCSI\s+sense\s+code", r"command\s+aborted"],
        metric_conditions={"datastore|totalLatency_average": ("gt", 20)},
        predicted_failure="VM performance degradation",
        failure_probability=0.7,
        recommended_actions=["Identify contention sources", "Consider Storage vMotion"],
    ),
    KnownPattern(
        id="memory-pressure",
        name="Memory Pressure",
        category=PatternCategory.MEMORY,
        description="Host memory overcommitment",
        severity=Severity.WARNING,
        log_patterns=[r"memory\s+balloon", r"swap\s+(in|out)"],
        metric_conditions={"mem|usage_average": ("gt", 90)},
        predicted_failure="VM performance degradation or crashes",
        failure_probability=0.6,
        recommended_actions=["Trigger DRS rebalancing", "Identify memory-hungry VMs"],
        auto_remediate=True,
    ),
    KnownPattern(
        id="memory-oom",
        name="Out of Memory",
        category=PatternCategory.MEMORY,
        description="System or VM out of memory",
        severity=Severity.CRITICAL,
        log_patterns=[r"Out\s+of\s+memory", r"OOM\s+kill"],
        metric_conditions={"mem|usage_average": ("gt", 98)},
        predicted_failure="VM or service crash",
        failure_probability=0.9,
        recommended_actions=["vMotion VMs to other hosts", "Investigate memory leak"],
    ),
    KnownPattern(
        id="network-link-down",
        name="Network Link Down",
        category=PatternCategory.NETWORK,
        description="Physical network link failure",
        severity=Severity.CRITICAL,
        log_patterns=[r"NIC\s+link\s+is\s+down", r"vmnic\d+.*link\s+down"],
        alert_names=["Host network link down"],
        predicted_failure="Network connectivity loss",
        failure_probability=0.95,
        recommended_actions=["Check physical cabling", "Verify switch port status"],
    ),
    KnownPattern(
        id="network-dvport-blocked",
        name="DVPort Blocked",
        category=PatternCategory.NETWORK,
        description="Distributed virtual port blocked",
        severity=Severity.WARNING,
        log_patterns=[r"DVPort.*blocked", r"port\s+block"],
        predicted_failure="VM network isolation",
        failure_probability=0.8,
        recommended_actions=["Check NSX security rules", "Review port group configuration"],
    ),
    KnownPattern(
        id="compute-cpu-contention",
        name="CPU Contention",
        category=PatternCategory.COMPUTE,
        description="High CPU ready time",
        severity=Severity.WARNING,
        metric_conditions={"cpu|ready_summation": ("gt", 5000), "cpu|usage_average": ("gt", 85)},
        predicted_failure="VM performance degradation",
        failure_probability=0.6,
        recommended_actions=["Trigger DRS rebalancing", "Review VM CPU reservations"],
        auto_remediate=True,
    ),
    KnownPattern(
        id="ha-failover",
        name="HA Failover Event",
        category=PatternCategory.AVAILABILITY,
        description="HA has restarted VMs due to host failure",
        severity=Severity.CRITICAL,
        log_patterns=[r"HA\s+failover", r"vSphere\s+HA\s+restarted"],
        predicted_failure="Check for underlying host issues",
        failure_probability=0.3,
        recommended_actions=["Investigate failed host", "Verify VMs restarted"],
    ),
    KnownPattern(
        id="capacity-datastore-full",
        name="Datastore Space Low",
        category=PatternCategory.STORAGE,
        description="Datastore running out of space",
        severity=Severity.WARNING,
        log_patterns=[r"datastore.*full", r"disk\s+space.*low"],
        metric_conditions={"diskspace|used_average": ("gt", 85)},
        predicted_failure="VM snapshot failures, provisioning blocked",
        failure_probability=0.7,
        recommended_actions=["Clean up old snapshots", "Remove orphaned VMDKs"],
        auto_remediate=True,
    ),
]


def _pattern_from_dict(data: dict[str, Any]) -> KnownPattern:
    """Build a KnownPattern from a config dict (e.g. parsed YAML).

    Accepts case-insensitive ``category``/``severity`` strings and
    ``metric_conditions`` whose values are ``[operator, threshold]`` pairs.
    """
    if "id" not in data or "name" not in data:
        raise ValueError("Custom pattern requires 'id' and 'name'")

    category = PatternCategory(str(data.get("category", "compute")).lower())
    severity = Severity(str(data.get("severity", "WARNING")).upper())

    metric_conditions: dict[str, tuple[str, float]] = {}
    for key, cond in (data.get("metric_conditions") or {}).items():
        operator, threshold = cond
        metric_conditions[key] = (str(operator), float(threshold))

    return KnownPattern(
        id=str(data["id"]),
        name=str(data["name"]),
        category=category,
        description=str(data.get("description", "")),
        severity=severity,
        log_patterns=list(data.get("log_patterns", [])),
        metric_conditions=metric_conditions,
        alert_names=list(data.get("alert_names", [])),
        predicted_failure=str(data.get("predicted_failure", "")),
        failure_probability=float(data.get("failure_probability", 0.5)),
        recommended_actions=list(data.get("recommended_actions", [])),
        auto_remediate=bool(data.get("auto_remediate", False)),
    )


def load_custom_patterns(path: str | Path) -> list[KnownPattern]:
    """Load extra patterns from a YAML file.

    The file may be either a top-level list of patterns or a mapping with a
    ``patterns:`` key. Invalid entries are skipped with a warning rather than
    aborting startup. Returns an empty list if the file does not exist.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("Custom patterns file not found", path=str(p))
        return []

    raw = yaml.safe_load(p.read_text()) or []
    items = raw.get("patterns", []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        logger.warning("Custom patterns file has no pattern list", path=str(p))
        return []

    patterns: list[KnownPattern] = []
    for item in items:
        try:
            patterns.append(_pattern_from_dict(item))
        except (ValueError, KeyError, TypeError) as e:
            logger.warning("Skipping invalid custom pattern", error=str(e), entry=str(item)[:200])

    logger.info("Loaded custom patterns", count=len(patterns), path=str(p))
    return patterns


class PatternMatcher:
    """Matches infrastructure state against known patterns."""

    def __init__(self, patterns: list[KnownPattern] | None = None):
        self.patterns = patterns or KNOWN_PATTERNS
        self._compiled_patterns: dict[str, list[re.Pattern]] = {}
        for pattern in self.patterns:
            self._compiled_patterns[pattern.id] = [
                re.compile(p, re.IGNORECASE) for p in pattern.log_patterns
            ]

    def match_logs(self, logs: list[LogEntry]) -> list[tuple[KnownPattern, list[LogEntry]]]:
        matches = []
        for pattern in self.patterns:
            compiled = self._compiled_patterns.get(pattern.id, [])
            if not compiled:
                continue

            matching_logs = []
            for log in logs:
                for regex in compiled:
                    if regex.search(log.text):
                        matching_logs.append(log)
                        break

            if matching_logs:
                matches.append((pattern, matching_logs))

        return matches

    def match_metrics(
        self, resources: list[ResourceHealth]
    ) -> list[tuple[KnownPattern, list[ResourceHealth]]]:
        matches = []
        for pattern in self.patterns:
            if not pattern.metric_conditions:
                continue

            matching_resources = []
            for resource in resources:
                for metric_key, (operator, threshold) in pattern.metric_conditions.items():
                    metric = resource.metrics.get(metric_key)
                    if not metric or metric.latest_value is None:
                        continue

                    value = metric.latest_value
                    if operator == "gt" and value > threshold:
                        matching_resources.append(resource)
                        break
                    elif operator == "lt" and value < threshold:
                        matching_resources.append(resource)
                        break

            if matching_resources:
                matches.append((pattern, matching_resources))

        return matches

    def match_alerts(self, alerts: list[Alert]) -> list[tuple[KnownPattern, list[Alert]]]:
        matches = []
        for pattern in self.patterns:
            if not pattern.alert_names:
                continue

            matching_alerts = []
            for alert in alerts:
                for alert_pattern in pattern.alert_names:
                    if alert_pattern.lower() in alert.name.lower():
                        matching_alerts.append(alert)
                        break

            if matching_alerts:
                matches.append((pattern, matching_alerts))

        return matches
