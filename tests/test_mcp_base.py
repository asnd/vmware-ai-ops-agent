"""
Tests for BaseMCPClient: SSE parsing, header correctness, retry on transport errors.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from vmware_ai_ops_agent.mcp_clients.ariaops import AriaOpsMCPClient
from vmware_ai_ops_agent.mcp_clients.base import BaseMCPClient, _parse_sse_body
from vmware_ai_ops_agent.mcp_clients.entrag import EntragMCPClient

# ---------------------------------------------------------------------------
# SSE body parser
# ---------------------------------------------------------------------------


class TestParseSseBody:
    def test_single_data_event(self):
        payload = {"jsonrpc": "2.0", "id": 1, "result": {"content": []}}
        text = f"data: {json.dumps(payload)}\n\n"
        assert _parse_sse_body(text) == payload

    def test_multi_event_returns_last(self):
        first = {"id": 1, "result": "first"}
        last = {"id": 2, "result": "last"}
        text = f"data: {json.dumps(first)}\n\ndata: {json.dumps(last)}\n\n"
        assert _parse_sse_body(text) == last

    def test_non_data_lines_ignored(self):
        payload = {"ok": True}
        text = f"event: message\nretry: 1000\ndata: {json.dumps(payload)}\n\n"
        assert _parse_sse_body(text) == payload

    def test_invalid_json_line_skipped(self):
        valid = {"ok": True}
        text = f"data: not-json\n\ndata: {json.dumps(valid)}\n\n"
        assert _parse_sse_body(text) == valid

    def test_empty_body_returns_none(self):
        assert _parse_sse_body("") is None


# ---------------------------------------------------------------------------
# Header negotiation (A2)
# ---------------------------------------------------------------------------


class TestBaseMCPClientHeaders:
    def _make_init_response(self, session_id: str = "s1") -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"mcp-session-id": session_id, "content-type": "application/json"}
        resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"serverInfo": {"name": "test"}, "protocolVersion": "2025-03-26"},
        }
        resp.raise_for_status = MagicMock()
        return resp

    @pytest.mark.asyncio
    async def test_connect_sends_accept_and_mcp_protocol_version(self):
        """connect() must include Accept and MCP-Protocol-Version in the session headers."""
        init_resp = self._make_init_response()
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=init_resp)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_http
            client = AriaOpsMCPClient(base_url="http://aria:8080", auth_token="tok")
            await client.connect()

            _, ctor_kwargs = mock_cls.call_args
            headers = ctor_kwargs.get("headers", {})
            assert "Accept" in headers
            assert "text/event-stream" in headers["Accept"]
            assert "MCP-Protocol-Version" in headers

    @pytest.mark.asyncio
    async def test_call_tool_sends_session_id_header(self):
        client = EntragMCPClient(base_url="http://entrag:8081")
        client._session_id = "session-xyz"

        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": '{"results":[]}'}]},
        }
        resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=resp)
        client._client = mock_http

        await client._call_tool("rag_query", {"query": "test"})

        _, call_kwargs = mock_http.post.call_args
        sent_headers = call_kwargs.get("headers", {})
        assert sent_headers.get("mcp-session-id") == "session-xyz"


# ---------------------------------------------------------------------------
# SSE response decoding
# ---------------------------------------------------------------------------


class TestDecodeResponse:
    def test_json_content_type_parsed_as_json(self):
        client = BaseMCPClient(base_url="http://x")
        resp = MagicMock()
        resp.headers = {"content-type": "application/json"}
        resp.json.return_value = {"ok": True}
        assert client._decode_response(resp) == {"ok": True}

    def test_sse_content_type_parsed_as_sse(self):
        client = BaseMCPClient(base_url="http://x")
        payload = {"jsonrpc": "2.0", "id": 1, "result": {}}
        resp = MagicMock()
        resp.headers = {"content-type": "text/event-stream"}
        resp.text = f"data: {json.dumps(payload)}\n\n"
        assert client._decode_response(resp) == payload


# ---------------------------------------------------------------------------
# Retry on transport errors (A3)
# ---------------------------------------------------------------------------


class TestCallToolRetry:
    @pytest.mark.asyncio
    async def test_retries_on_timeout_then_succeeds(self):
        """_call_tool should retry on TimeoutException and succeed on the third attempt."""
        client = AriaOpsMCPClient(base_url="http://aria:8080")
        client._client = AsyncMock()
        client._session_id = "sess"

        good_resp = MagicMock()
        good_resp.status_code = 200
        good_resp.headers = {"content-type": "application/json"}
        good_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": '{"resources":[]}'}]},
        }
        good_resp.raise_for_status = MagicMock()

        call_count = 0

        async def flaky_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise httpx.TimeoutException("timeout")
            return good_resp

        client._client.post = flaky_post

        result = await client._call_tool("list_resources", {})
        assert call_count == 3
        assert result == {"resources": []}

    @pytest.mark.asyncio
    async def test_does_not_retry_runtime_error(self):
        """MCP-level errors (RuntimeError) must not be retried."""
        client = EntragMCPClient(base_url="http://entrag:8081")
        client._client = AsyncMock()
        client._session_id = "sess"

        error_resp = MagicMock()
        error_resp.status_code = 200
        error_resp.headers = {"content-type": "application/json"}
        error_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "Tool not found"},
        }
        error_resp.raise_for_status = MagicMock()

        call_count = 0

        async def single_error_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return error_resp

        client._client.post = single_error_post

        with pytest.raises(RuntimeError, match="Tool not found"):
            await client._call_tool("nonexistent_tool", {})

        assert call_count == 1, "RuntimeError from MCP must not be retried"

    @pytest.mark.asyncio
    async def test_not_connected_raises_immediately(self):
        client = EntragMCPClient(base_url="http://entrag:8081")
        with pytest.raises(RuntimeError, match="not connected"):
            await client._call_tool("rag_query", {"query": "test"})


# ---------------------------------------------------------------------------
# RPC ID sequencing
# ---------------------------------------------------------------------------


class TestRpcIdSequencing:
    def test_ids_are_sequential_and_unique(self):
        """Each _call_tool invocation must use a different RPC id."""
        client = BaseMCPClient(base_url="http://x")
        ids = [next(client._rpc_counter) for _ in range(5)]
        assert ids == list(range(1, 6))
        assert len(set(ids)) == 5
