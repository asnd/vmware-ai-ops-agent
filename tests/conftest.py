"""
Pytest configuration and fixtures for VMware AI Ops Agent tests.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from vmware_ai_ops_agent.config import (
    Settings,
    VROpsConfig,
    VRLIConfig,
    LLMConfig,
    VCenterConfig,
    AgentConfig,
    VectorDBConfig,
    NotificationsConfig,
    MetricsConfig,
    LoggingConfig,
    KnowledgeBaseConfig,
)
from vmware_ai_ops_agent.collectors.models import (
    ResourceKind,
    ResourceHealth,
    VMwareResource,
    Alert,
    AlertSeverity,
    InfrastructureState,
)
from vmware_ai_ops_agent.analysis.models import Urgency


@pytest.fixture
def test_settings() -> Settings:
    """Create test settings with mock values."""
    return Settings(
        vrops=VROpsConfig(host="test-vrops.local", username="test", password="test"),
        vrli=VRLIConfig(host="test-vrli.local", username="test", password="test"),
        llm=LLMConfig(endpoint="http://localhost:8000/v1", api_key="test-key"),
        vcenter=VCenterConfig(host="test-vcenter.local", username="test", password="test", dry_run=True),
        vector_db=VectorDBConfig(persist_directory="/tmp/test-chromadb"),
        agent=AgentConfig(cycle_interval=60),
        notifications=NotificationsConfig(),
        metrics=MetricsConfig(enabled=False),
        logging=LoggingConfig(level="DEBUG"),
        knowledge_base=KnowledgeBaseConfig(runbooks_dir="/tmp/runbooks", kb_cache_dir="/tmp/kb_cache"),
    )


@pytest.fixture
def sample_resource() -> ResourceHealth:
    """Create a sample resource health object."""
    return ResourceHealth(
        resource=VMwareResource(
            id="vm-123",
            name="test-vm-01",
            kind=ResourceKind.VIRTUAL_MACHINE,
            moref="vm-123",
        ),
        health_score=75.0,
        metrics={
            "cpu|usage_average": 45.0,
            "mem|usage_average": 60.0,
            "disk|commandsAveraged_average": 10.0,
        },
    )


@pytest.fixture
def sample_alert() -> Alert:
    """Create a sample alert."""
    return Alert(
        id="alert-456",
        name="High CPU Usage",
        severity=AlertSeverity.WARNING,
        status="ACTIVE",
        resource_id="vm-123",
        resource_name="test-vm-01",
        message="CPU usage exceeded 80%",
    )


@pytest.fixture
def sample_infrastructure_state(sample_resource: ResourceHealth, sample_alert: Alert) -> InfrastructureState:
    """Create a sample infrastructure state."""
    state = InfrastructureState()
    state.resources = [sample_resource]
    state.alerts = [sample_alert]
    return state


@pytest.fixture
def mock_vrops_client():
    """Create a mock vROps client."""
    client = AsyncMock()
    client.collect_all = AsyncMock(return_value=([], [], [], []))
    return client


@pytest.fixture
def mock_vrli_client():
    """Create a mock vRLI client."""
    client = AsyncMock()
    client.collect_all = AsyncMock(return_value=([], []))
    return client


@pytest.fixture
def mock_vcenter_client():
    """Create a mock vCenter client."""
    client = AsyncMock()
    client.config = MagicMock()
    client.config.dry_run = True
    client.vmotion_vm = AsyncMock(return_value={"dry_run": True, "action": "vmotion"})
    client.storage_vmotion_vm = AsyncMock(return_value={"dry_run": True, "action": "storage_vmotion"})
    client.find_best_target_host = AsyncMock(return_value="host-01")
    client.find_best_target_datastore = AsyncMock(return_value="datastore-01")
    return client


@pytest.fixture
def mock_llm_engine():
    """Create a mock LLM analysis engine."""
    from vmware_ai_ops_agent.analysis.models import AnalysisResult

    engine = AsyncMock()
    engine.analyze_infrastructure = AsyncMock(
        return_value=AnalysisResult(
            summary="Test analysis complete",
            urgency=Urgency.LOW,
            findings=[],
            predictions=[],
        )
    )
    return engine
