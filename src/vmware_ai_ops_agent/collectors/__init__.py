"""
Data collectors for VMware infrastructure.

The VROpsCollector is retained for backward compatibility but the preferred
data path is via the AriaOpsMCPClient in mcp_clients/.
"""

from .models import (
    Alert,
    Anomaly,
    LogEntry,
    Metric,
    Recommendation,
    ResourceHealth,
)
from .vrli import VRLICollector
from .vrops import VROpsCollector

__all__ = [
    "VROpsCollector",
    "VRLICollector",
    "ResourceHealth",
    "Alert",
    "Metric",
    "LogEntry",
    "Recommendation",
    "Anomaly",
]
