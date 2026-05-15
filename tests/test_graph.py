"""
Tests for the refactored LangGraph workflow with MCP client integration.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from vmware_ai_ops_agent.analysis.models import AnalysisResult, Urgency
from vmware_ai_ops_agent.collectors.models import (
    Alert,
    InfrastructureState,
    ResourceHealth,
    ResourceIdentifier,
    ResourceKind,
    Severity,
)
from vmware_ai_ops_agent.correlation.engine import CorrelationEngine
from vmware_ai_ops_agent.graph import create_agent_graph


class TestAgentGraph:
    """Tests for the refactored agent graph."""

    @pytest.fixture
    def mock_collector(self):
        """Mock collector function."""
        state = InfrastructureState()
        state.resources = [
            ResourceHealth(
                resource=ResourceIdentifier(
                    id="vm-1", name="test-vm", kind=ResourceKind.VIRTUAL_MACHINE
                ),
                health_state="RED",
                health_score=20.0,
            )
        ]
        state.alerts = [
            Alert(
                id="alert-1",
                alert_definition_id="def-1",
                name="Critical CPU",
                description="CPU usage critical",
                severity=Severity.CRITICAL,
                status="ACTIVE",
                resource=ResourceIdentifier(
                    id="vm-1", name="test-vm", kind=ResourceKind.VIRTUAL_MACHINE
                ),
                start_time="2024-01-01T00:00:00Z",
            )
        ]
        return AsyncMock(return_value=state)

    @pytest.fixture
    def mock_correlation_engine(self):
        """Mock correlation engine."""
        engine = MagicMock(spec=CorrelationEngine)

        class MockIssue:
            pattern = MagicMock(name="CPU_CONTENTION")
            description = "Critical CPU contention detected on test-vm"
            severity = Severity.CRITICAL
            affected_resources = ["vm-1"]
            recommended_actions = ["vmotion"]

        class MockResult:
            issues = [MockIssue()]
            resources_analyzed = 1
            alerts_analyzed = 1

        engine.correlate.return_value = MockResult()
        return engine

    @pytest.fixture
    def mock_knowledge_base(self):
        """Mock knowledge base."""
        kb = AsyncMock()
        kb.search_similar = AsyncMock(return_value=[])
        return kb

    @pytest.fixture
    def mock_llm_engine(self):
        """Mock LLM engine."""
        engine = AsyncMock()
        engine.analyze_infrastructure = AsyncMock(
            return_value=AnalysisResult(
                id="analysis-1",
                summary="Critical CPU contention on test-vm",
                urgency=Urgency.HIGH,
                insights=["VM needs migration"],
                predicted_failures=[],
            )
        )
        return engine

    @pytest.fixture
    def mock_remediator(self):
        """Mock remediator function."""
        return AsyncMock(return_value={"status": "disabled"})

    @pytest.fixture
    def mock_entrag_client(self):
        """Mock EntRAG MCP client."""
        client = AsyncMock()
        client.search = AsyncMock(
            return_value=[
                {
                    "title": "KB123 - CPU Contention Resolution",
                    "link": "https://kb.broadcom.com/123",
                    "snippet": "To resolve CPU contention, consider vMotion...",
                    "section_type": "resolution",
                    "score": "0.9",
                }
            ]
        )
        return client

    @pytest.fixture
    def mock_ariaops_client(self):
        """Mock AriaOps MCP client."""
        client = AsyncMock()
        client.get_capacity_remaining = AsyncMock(
            return_value={
                "resource_name": "test-vm",
                "remaining_capacity": 25.0,
                "time_remaining": 14,
            }
        )
        return client

    @pytest.mark.asyncio
    async def test_graph_full_flow_with_mcp(
        self,
        mock_collector,
        mock_correlation_engine,
        mock_knowledge_base,
        mock_llm_engine,
        mock_remediator,
        mock_entrag_client,
        mock_ariaops_client,
    ):
        """Full graph flow should use MCP clients for enrichment."""
        graph = create_agent_graph(
            collector_func=mock_collector,
            correlation_engine=mock_correlation_engine,
            knowledge_base=mock_knowledge_base,
            llm_engine=mock_llm_engine,
            remediator_func=mock_remediator,
            entrag_client=mock_entrag_client,
            ariaops_client=mock_ariaops_client,
        )

        initial_state = {
            "infrastructure_state": None,
            "correlation_result": None,
            "analysis_result": None,
            "kb_results": None,
            "search_results": None,
            "capacity_data": None,
            "remediation_status": None,
            "errors": [],
        }

        result = await graph.ainvoke(initial_state)

        # Verify collector was called
        mock_collector.assert_called_once()

        # Verify correlation was run
        mock_correlation_engine.correlate.assert_called_once()

        # Verify EntRAG was called for KB enrichment
        mock_entrag_client.search.assert_called_once()

        # Verify analysis received enriched context
        mock_llm_engine.analyze_infrastructure.assert_called_once()
        call_args = mock_llm_engine.analyze_infrastructure.call_args
        context = call_args.kwargs.get("context", "")
        assert "Knowledge Base Articles" in context or "KB123" in context

        # Verify final state
        assert result.get("analysis_result") is not None
        assert result["analysis_result"].urgency == Urgency.HIGH

    @pytest.mark.asyncio
    async def test_graph_no_issues_ends_early(
        self,
        mock_collector,
        mock_knowledge_base,
        mock_llm_engine,
        mock_remediator,
    ):
        """Graph should end after correlate if no issues found."""
        # Empty correlation result
        engine = MagicMock(spec=CorrelationEngine)

        class EmptyResult:
            issues = []
            resources_analyzed = 1
            alerts_analyzed = 0

        engine.correlate.return_value = EmptyResult()

        graph = create_agent_graph(
            collector_func=mock_collector,
            correlation_engine=engine,
            knowledge_base=mock_knowledge_base,
            llm_engine=mock_llm_engine,
            remediator_func=mock_remediator,
        )

        initial_state = {
            "infrastructure_state": None,
            "correlation_result": None,
            "analysis_result": None,
            "kb_results": None,
            "search_results": None,
            "capacity_data": None,
            "remediation_status": None,
            "errors": [],
        }

        result = await graph.ainvoke(initial_state)

        # Analysis should NOT have been called
        mock_llm_engine.analyze_infrastructure.assert_not_called()
        assert result.get("analysis_result") is None

    @pytest.mark.asyncio
    async def test_graph_without_mcp_clients(
        self,
        mock_collector,
        mock_correlation_engine,
        mock_knowledge_base,
        mock_llm_engine,
        mock_remediator,
    ):
        """Graph should work without MCP clients (graceful degradation)."""
        graph = create_agent_graph(
            collector_func=mock_collector,
            correlation_engine=mock_correlation_engine,
            knowledge_base=mock_knowledge_base,
            llm_engine=mock_llm_engine,
            remediator_func=mock_remediator,
            entrag_client=None,
            ariaops_client=None,
        )

        initial_state = {
            "infrastructure_state": None,
            "correlation_result": None,
            "analysis_result": None,
            "kb_results": None,
            "search_results": None,
            "capacity_data": None,
            "remediation_status": None,
            "errors": [],
        }

        result = await graph.ainvoke(initial_state)

        # Should still produce analysis from KB alone
        mock_llm_engine.analyze_infrastructure.assert_called_once()
        assert result.get("analysis_result") is not None

    @pytest.mark.asyncio
    async def test_graph_collection_failure(self, mock_remediator):
        """Graph should handle collection failures gracefully."""
        failing_collector = AsyncMock(side_effect=RuntimeError("Connection refused"))

        engine = MagicMock(spec=CorrelationEngine)
        kb = AsyncMock()
        llm = AsyncMock()

        graph = create_agent_graph(
            collector_func=failing_collector,
            correlation_engine=engine,
            knowledge_base=kb,
            llm_engine=llm,
            remediator_func=mock_remediator,
        )

        initial_state = {
            "infrastructure_state": None,
            "correlation_result": None,
            "analysis_result": None,
            "kb_results": None,
            "search_results": None,
            "capacity_data": None,
            "remediation_status": None,
            "errors": [],
        }

        result = await graph.ainvoke(initial_state)

        assert len(result.get("errors", [])) > 0
        assert "Collection failed" in result["errors"][0]
