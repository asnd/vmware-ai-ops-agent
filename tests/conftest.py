"""
Pytest configuration and fixtures for VMware AI Ops Agent tests.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from vmware_ai_ops_agent.analysis.models import Urgency
from vmware_ai_ops_agent.collectors.models import (
    Alert,
    InfrastructureState,
    Metric,
    ResourceHealth,
    ResourceIdentifier,
    ResourceKind,
    Severity,
)
from vmware_ai_ops_agent.config import (
    AgentConfig,
    AriaOpsMCPConfig,
    EntragMCPConfig,
    KnowledgeBaseConfig,
    LLMConfig,
    LoggingConfig,
    MetricsConfig,
    NotificationsConfig,
    Settings,
    VCenterConfig,
    VectorDBConfig,
    VRLIConfig,
    VROpsConfig,
)


@pytest.fixture
def test_settings() -> Settings:
    """Create test settings with mock values."""
    return Settings(
        vrops=VROpsConfig(host="test-vrops.local", username="test", password="test"),
        vrli=VRLIConfig(host="test-vrli.local", username="test", password="test"),
        llm=LLMConfig(endpoint="http://localhost:8000/v1", api_key="test-key"),
        vcenter=VCenterConfig(
            host="test-vcenter.local",
            username="test",
            password="test",
            dry_run=True,
        ),
        vector_db=VectorDBConfig(persist_directory="/tmp/test-faiss"),
        agent=AgentConfig(cycle_interval=60),
        notifications=NotificationsConfig(),
        metrics=MetricsConfig(enabled=False),
        logging=LoggingConfig(level="DEBUG"),
        knowledge_base=KnowledgeBaseConfig(
            runbooks_dir="/tmp/runbooks", kb_cache_dir="/tmp/kb_cache"
        ),
        ariaops_mcp=AriaOpsMCPConfig(
            url="http://localhost:8080/mcp",
            enabled=False,
        ),
        entrag_mcp=EntragMCPConfig(
            url="http://localhost:8081/mcp",
            enabled=False,
        ),
    )


@pytest.fixture
def sample_resource() -> ResourceHealth:
    """Create a sample resource health object."""
    return ResourceHealth(
        resource=ResourceIdentifier(
            id="vm-123",
            name="test-vm-01",
            kind=ResourceKind.VIRTUAL_MACHINE,
        ),
        health_state="GREEN",
        health_score=75.0,
        metrics={
            "cpu|usage_average": Metric(
                resource_id="vm-123",
                resource_name="test-vm-01",
                stat_key="cpu|usage_average",
                values=[45.0],
            ),
            "mem|usage_average": Metric(
                resource_id="vm-123",
                resource_name="test-vm-01",
                stat_key="mem|usage_average",
                values=[60.0],
            ),
        },
    )


@pytest.fixture
def sample_alert(sample_resource) -> Alert:
    """Create a sample alert."""
    return Alert(
        id="alert-456",
        alert_definition_id="def-1",
        name="High CPU Usage",
        description="CPU usage exceeded 80%",
        severity=Severity.WARNING,
        status="ACTIVE",
        resource=sample_resource.resource,
        start_time=datetime.utcnow(),
    )


@pytest.fixture
def sample_infrastructure_state(
    sample_resource: ResourceHealth, sample_alert: Alert
) -> InfrastructureState:
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
    client.storage_vmotion_vm = AsyncMock(
        return_value={"dry_run": True, "action": "storage_vmotion"}
    )
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
            id="analysis-123",
            summary="Test analysis complete",
            urgency=Urgency.LOW,
            insights=[],
            predicted_failures=[],
        )
    )
    return engine


@pytest.fixture
def mock_ariaops_mcp_client():
    """Create a mock AriaOps MCP client."""
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.collect_all = AsyncMock(return_value=([], [], [], []))
    client.list_alerts = AsyncMock(return_value=[])
    client.list_resources = AsyncMock(return_value=[])
    client.get_capacity_remaining = AsyncMock(
        return_value={"remaining_capacity": 50.0, "time_remaining": 90}
    )
    client.mark_resources_maintained = AsyncMock(return_value={"status": "success"})
    return client


@pytest.fixture
def mock_entrag_mcp_client():
    """Create a mock EntRAG MCP client."""
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.search = AsyncMock(
        return_value=[
            {
                "title": "KB Test Article",
                "link": "https://kb.broadcom.com/test",
                "snippet": "Test content",
                "section_type": "resolution",
                "score": "0.8",
            }
        ]
    )
    client.search_kb = AsyncMock(
        return_value=[
            {
                "title": "KB Test Article",
                "url": "https://kb.broadcom.com/test",
                "content": "Test content",
                "section_type": "resolution",
                "relevance_score": 0.8,
            }
        ]
    )
    return client
