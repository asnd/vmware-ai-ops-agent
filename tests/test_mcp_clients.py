"""
Tests for MCP client adapters: AriaOpsMCPClient and EntragMCPClient.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vmware_ai_ops_agent.collectors.models import (
    Alert,
    HealthState,
    ResourceHealth,
    ResourceKind,
    Severity,
)
from vmware_ai_ops_agent.mcp_clients.ariaops import AriaOpsMCPClient
from vmware_ai_ops_agent.mcp_clients.entrag import EntragMCPClient

# --- Fixtures ---


def _make_mcp_response(result_data: dict | list) -> dict:
    """Build a valid MCP JSON-RPC response."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(result_data)}]},
    }


def _make_init_response() -> dict:
    """Build a valid MCP initialize response."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-03-26",
            "serverInfo": {"name": "test-server", "version": "1.0.0"},
            "capabilities": {},
        },
    }


class TestAriaOpsMCPClient:
    """Tests for AriaOpsMCPClient."""

    @pytest.fixture
    def mock_transport(self):
        """Create a mock httpx transport for testing."""
        transport = AsyncMock()
        return transport

    @pytest.fixture
    def client(self):
        """Create client instance (not connected)."""
        return AriaOpsMCPClient(
            base_url="http://localhost:8080",
            auth_token="test-token",
            timeout=30.0,
        )

    @pytest.mark.asyncio
    async def test_connect_sends_initialize(self, client):
        """Client should send initialize + initialized on connect."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _make_init_response()
        mock_response.headers = {"mcp-session-id": "session-123"}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            with patch("httpx.AsyncClient.__init__", return_value=None):
                client._client = AsyncMock()
                client._client.post = AsyncMock(return_value=mock_response)

                await client.connect()

                assert client._session_id == "session-123"
                assert client._client.post.call_count == 2  # init + initialized

    @pytest.mark.asyncio
    async def test_call_tool_parses_json_content(self, client):
        """_call_tool should parse JSON from text content blocks."""
        client._client = AsyncMock()
        client._session_id = "test-session"

        expected_data = {"resources": [{"id": "vm-1", "name": "test-vm"}]}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _make_mcp_response(expected_data)
        mock_response.raise_for_status = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        result = await client._call_tool("list_resources", {"resource_kind": "VirtualMachine"})

        assert result == expected_data

    @pytest.mark.asyncio
    async def test_call_tool_raises_on_error(self, client):
        """_call_tool should raise RuntimeError on MCP error response."""
        client._client = AsyncMock()
        client._session_id = "test-session"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "Resource not found"},
        }
        mock_response.raise_for_status = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(RuntimeError, match="Resource not found"):
            await client._call_tool("get_resource", {"resource_id": "nonexistent"})

    @pytest.mark.asyncio
    async def test_list_alerts(self, client):
        """list_alerts should return parsed alert list."""
        client._client = AsyncMock()
        client._session_id = "test-session"

        alerts_data = {
            "alerts": [
                {
                    "alertId": "alert-1",
                    "alertDefinitionName": "High CPU",
                    "alertCriticality": "CRITICAL",
                    "status": "ACTIVE",
                    "resource": {
                        "identifier": "vm-1",
                        "resourceKey": {"name": "test-vm", "resourceKindKey": "VirtualMachine"},
                    },
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _make_mcp_response(alerts_data)
        mock_response.raise_for_status = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        result = await client.list_alerts(status="ACTIVE")
        assert len(result) == 1
        assert result[0]["alertId"] == "alert-1"

    @pytest.mark.asyncio
    async def test_collect_all_returns_models(self, client):
        """collect_all should return properly typed model instances."""
        client._client = AsyncMock()
        client._session_id = "test-session"

        # Mock list_resources calls for each kind
        resources_response = _make_mcp_response(
            {
                "resources": [
                    {
                        "identifier": "vm-1",
                        "resourceKey": {
                            "name": "test-vm",
                            "resourceKindKey": "VirtualMachine",
                            "adapterKindKey": "VMWARE",
                        },
                        "health": 85.0,
                    }
                ]
            }
        )

        alerts_response = _make_mcp_response(
            {
                "alerts": [
                    {
                        "alertId": "alert-1",
                        "alertDefinitionId": "def-1",
                        "alertDefinitionName": "Test Alert",
                        "alertDefinitionDescription": "Test",
                        "alertCriticality": "WARNING",
                        "status": "ACTIVE",
                        "startTimeUTC": 1700000000000,
                        "resource": {
                            "identifier": "vm-1",
                            "resourceKey": {"name": "test-vm", "resourceKindKey": "VirtualMachine"},
                        },
                        "symptoms": [],
                    }
                ]
            }
        )

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()

            # Determine which response based on the request body
            body = kwargs.get("json", {})
            params = body.get("params", {})
            tool_name = params.get("name", "")

            if tool_name == "list_alerts":
                mock_resp.json.return_value = alerts_response
            else:
                mock_resp.json.return_value = resources_response
            return mock_resp

        client._client.post = mock_post

        resources, alerts, recommendations, anomalies = await client.collect_all(
            resource_kinds=["VirtualMachine"]
        )

        assert len(resources) >= 1
        assert isinstance(resources[0], ResourceHealth)
        assert resources[0].resource.name == "test-vm"
        assert resources[0].health_state == HealthState.GREEN

        assert len(alerts) >= 1
        assert isinstance(alerts[0], Alert)
        assert alerts[0].severity == Severity.WARNING

    @pytest.mark.asyncio
    async def test_get_capacity_remaining(self, client):
        """get_capacity_remaining should call the right tool."""
        client._client = AsyncMock()
        client._session_id = "test-session"

        cap_data = {"resource_name": "cluster-1", "remaining_capacity": 45.2, "time_remaining": 120}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _make_mcp_response(cap_data)
        mock_response.raise_for_status = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        result = await client.get_capacity_remaining("cluster-1")
        assert result["remaining_capacity"] == 45.2

    @pytest.mark.asyncio
    async def test_mark_resources_maintained(self, client):
        """mark_resources_maintained should call write_ops tool."""
        client._client = AsyncMock()
        client._session_id = "test-session"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _make_mcp_response({"status": "success"})
        mock_response.raise_for_status = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        result = await client.mark_resources_maintained(["host-1"], duration_minutes=120)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_disconnect(self, client):
        """disconnect should close the client."""
        mock_http_client = AsyncMock()
        client._client = mock_http_client
        client._session_id = "test-session"

        await client.disconnect()

        mock_http_client.aclose.assert_called_once()
        assert client._client is None
        assert client._session_id is None

    def test_parse_resource_health_green(self, client):
        """Resources with health >= 75 should be GREEN."""
        data = {
            "identifier": "vm-1",
            "resourceKey": {
                "name": "healthy-vm",
                "resourceKindKey": "VirtualMachine",
                "adapterKindKey": "VMWARE",
            },
            "health": 90.0,
        }
        result = client._parse_resource_health(data)
        assert result is not None
        assert result.health_state == HealthState.GREEN
        assert result.resource.kind == ResourceKind.VIRTUAL_MACHINE

    def test_parse_resource_health_red(self, client):
        """Resources with health < 25 should be RED."""
        data = {
            "identifier": "vm-2",
            "resourceKey": {
                "name": "critical-vm",
                "resourceKindKey": "VirtualMachine",
                "adapterKindKey": "VMWARE",
            },
            "health": 10.0,
        }
        result = client._parse_resource_health(data)
        assert result is not None
        assert result.health_state == HealthState.RED

    def test_parse_alert(self, client):
        """Alerts should be parsed correctly."""
        data = {
            "alertId": "alert-99",
            "alertDefinitionId": "def-99",
            "alertDefinitionName": "Disk Latency High",
            "alertDefinitionDescription": "Disk latency exceeded threshold",
            "alertCriticality": "CRITICAL",
            "status": "ACTIVE",
            "startTimeUTC": 1700000000000,
            "resource": {
                "identifier": "ds-1",
                "resourceKey": {"name": "shared-ds", "resourceKindKey": "Datastore"},
            },
            "symptoms": [
                {
                    "symptomDefinitionId": "sym-1",
                    "symptomName": "High Latency",
                    "severity": "CRITICAL",
                    "state": "ACTIVE",
                    "message": "Read latency > 50ms",
                    "startTimeUTC": 1700000000000,
                }
            ],
        }
        result = client._parse_alert(data)
        assert result is not None
        assert result.id == "alert-99"
        assert result.severity == Severity.CRITICAL
        assert result.resource.kind == ResourceKind.DATASTORE
        assert len(result.symptoms) == 1


class TestEntragMCPClient:
    """Tests for EntragMCPClient."""

    @pytest.fixture
    def client(self):
        """Create client instance (not connected)."""
        return EntragMCPClient(
            base_url="http://localhost:8081",
            auth_token="test-token",
            timeout=30.0,
        )

    @pytest.mark.asyncio
    async def test_connect(self, client):
        """Client should successfully initialize MCP session."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _make_init_response()
        mock_response.headers = {"mcp-session-id": "entrag-session-1"}
        mock_response.raise_for_status = MagicMock()

        # Pre-set the _client so connect() uses it instead of creating new
        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=mock_response)

        # Patch httpx.AsyncClient to return our mock
        with patch("httpx.AsyncClient", return_value=mock_http_client):
            await client.connect()
            assert client._session_id == "entrag-session-1"
            assert mock_http_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_search_kb_returns_results(self, client):
        """search_kb should return structured KB results."""
        client._client = AsyncMock()
        client._session_id = "test-session"

        kb_data = {
            "results": [
                {
                    "title": "KB123456 - ESXi APD Troubleshooting",
                    "url": "https://kb.broadcom.com/123456",
                    "content": "All Paths Down (APD) occurs when...",
                    "section_type": "resolution",
                    "relevance_score": 0.92,
                    "article_number": "KB123456",
                },
                {
                    "title": "KB789012 - Storage Connectivity",
                    "url": "https://kb.broadcom.com/789012",
                    "content": "Check iSCSI/FC connectivity...",
                    "section_type": "cause",
                    "relevance_score": 0.78,
                    "article_number": "KB789012",
                },
            ]
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _make_mcp_response(kb_data)
        mock_response.raise_for_status = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        results = await client.search_kb("ESXi APD all paths down")
        assert len(results) == 2
        assert results[0]["title"] == "KB123456 - ESXi APD Troubleshooting"
        assert results[0]["relevance_score"] == 0.92

    @pytest.mark.asyncio
    async def test_search_compatible_format(self, client):
        """search() should return title/link/snippet format."""
        client._client = AsyncMock()
        client._session_id = "test-session"

        kb_data = {
            "results": [
                {
                    "title": "KB123 - Test Article",
                    "url": "https://kb.broadcom.com/123",
                    "content": "This is the article content about the issue",
                    "section_type": "resolution",
                    "relevance_score": 0.85,
                },
            ]
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _make_mcp_response(kb_data)
        mock_response.raise_for_status = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        results = await client.search("test query")
        assert len(results) == 1
        assert "title" in results[0]
        assert "link" in results[0]
        assert "snippet" in results[0]
        assert results[0]["link"] == "https://kb.broadcom.com/123"

    @pytest.mark.asyncio
    async def test_search_kb_empty_results(self, client):
        """search_kb should return empty list when no results."""
        client._client = AsyncMock()
        client._session_id = "test-session"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _make_mcp_response({"results": []})
        mock_response.raise_for_status = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        results = await client.search_kb("nonexistent topic xyz")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_kb_raw_text_fallback(self, client):
        """search_kb should handle raw text responses gracefully."""
        client._client = AsyncMock()
        client._session_id = "test-session"

        mock_response = MagicMock()
        mock_response.status_code = 200
        # Return non-JSON text content
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "plain text result without JSON"}]},
        }
        mock_response.raise_for_status = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        results = await client.search_kb("test query")
        assert len(results) == 1
        assert "plain text result" in results[0].get("content", "")

    @pytest.mark.asyncio
    async def test_get_ingestion_status(self, client):
        """get_ingestion_status should return index health info."""
        client._client = AsyncMock()
        client._session_id = "test-session"

        status_data = {"total_documents": 1500, "index_healthy": True, "last_updated": "2024-01-15"}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _make_mcp_response(status_data)
        mock_response.raise_for_status = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        result = await client.get_ingestion_status()
        assert result["total_documents"] == 1500
        assert result["index_healthy"] is True

    @pytest.mark.asyncio
    async def test_disconnect(self, client):
        """disconnect should close the HTTP client."""
        mock_http_client = AsyncMock()
        client._client = mock_http_client
        client._session_id = "entrag-session"

        await client.disconnect()

        mock_http_client.aclose.assert_called_once()
        assert client._client is None
        assert client._session_id is None

    @pytest.mark.asyncio
    async def test_call_tool_not_connected(self, client):
        """_call_tool should raise if client not connected."""
        with pytest.raises(RuntimeError, match="not connected"):
            await client._call_tool("rag_query", {"query": "test"})
