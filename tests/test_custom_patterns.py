"""
Tests for config-driven custom correlation patterns.
"""

from datetime import datetime

from vmware_ai_ops_agent.collectors.models import (
    InfrastructureState,
    LogEntry,
    Severity,
)
from vmware_ai_ops_agent.correlation.engine import CorrelationEngine
from vmware_ai_ops_agent.correlation.patterns import (
    KNOWN_PATTERNS,
    PatternCategory,
    load_custom_patterns,
)

CUSTOM_YAML = """
patterns:
  - id: custom-vmfs-heap
    name: VMFS Heap Exhaustion
    category: storage
    description: VMFS heap nearing limit
    severity: CRITICAL
    log_patterns:
      - "VMFS heap exhausted"
    metric_conditions:
      "mem|usage_average": ["gt", 99]
    alert_names:
      - "VMFS heap"
    predicted_failure: Datastore operations stall
    failure_probability: 0.8
    recommended_actions:
      - Increase VMFS3.MaxHeapSizeMB
    auto_remediate: true
"""


def _write(tmp_path, text: str):
    path = tmp_path / "patterns.yaml"
    path.write_text(text)
    return path


def test_load_custom_patterns_parses_fields(tmp_path):
    patterns = load_custom_patterns(_write(tmp_path, CUSTOM_YAML))

    assert len(patterns) == 1
    p = patterns[0]
    assert p.id == "custom-vmfs-heap"
    assert p.category == PatternCategory.STORAGE
    assert p.severity == Severity.CRITICAL
    assert p.log_patterns == ["VMFS heap exhausted"]
    # [operator, threshold] list is converted to a (str, float) tuple
    assert p.metric_conditions == {"mem|usage_average": ("gt", 99.0)}
    assert p.failure_probability == 0.8
    assert p.auto_remediate is True


def test_load_custom_patterns_missing_file_returns_empty(tmp_path):
    assert load_custom_patterns(tmp_path / "does-not-exist.yaml") == []


def test_load_custom_patterns_skips_invalid_entries(tmp_path):
    bad = """
patterns:
  - name: missing-id
    category: storage
  - id: good
    name: Good Pattern
    category: compute
"""
    patterns = load_custom_patterns(_write(tmp_path, bad))
    assert [p.id for p in patterns] == ["good"]


def test_load_custom_patterns_accepts_top_level_list(tmp_path):
    as_list = """
- id: top-level
  name: Top Level
  category: network
  severity: WARNING
"""
    patterns = load_custom_patterns(_write(tmp_path, as_list))
    assert len(patterns) == 1
    assert patterns[0].category == PatternCategory.NETWORK


def test_engine_uses_custom_patterns(tmp_path):
    custom = load_custom_patterns(_write(tmp_path, CUSTOM_YAML))
    engine = CorrelationEngine(patterns=list(KNOWN_PATTERNS) + custom)

    state = InfrastructureState()
    state.recent_logs = [
        LogEntry(
            id="log-1",
            timestamp=datetime.utcnow(),
            source="esxi-01",
            source_type="esxi",
            text="VMFS heap exhausted on datastore ds-01",
        )
    ]

    result = engine.correlate(state)

    matched = [i for i in result.issues if i.pattern and i.pattern.id == "custom-vmfs-heap"]
    assert len(matched) == 1
    assert matched[0].severity == Severity.CRITICAL


def test_agent_merges_custom_patterns(test_settings, tmp_path):
    """The agent constructor merges custom patterns into its correlation engine."""
    from vmware_ai_ops_agent.agent import VMwareAIOpsAgent
    from vmware_ai_ops_agent.config import CorrelationConfig

    path = _write(tmp_path, CUSTOM_YAML)
    test_settings.correlation = CorrelationConfig(custom_patterns_file=str(path))

    agent = VMwareAIOpsAgent(test_settings)

    ids = [p.id for p in agent.correlation_engine.pattern_matcher.patterns]
    assert "custom-vmfs-heap" in ids  # custom pattern merged in
    assert "storage-apd" in ids  # built-ins still present


def test_agent_without_custom_patterns_uses_builtins(test_settings):
    """With no custom file configured, only the built-in patterns are used."""
    from vmware_ai_ops_agent.agent import VMwareAIOpsAgent

    agent = VMwareAIOpsAgent(test_settings)

    ids = [p.id for p in agent.correlation_engine.pattern_matcher.patterns]
    assert len(ids) == len(KNOWN_PATTERNS)
    assert "custom-vmfs-heap" not in ids
