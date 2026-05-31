"""
Shared MCP Streamable HTTP client base.

Handles session lifecycle, Accept/SSE negotiation, request-ID sequencing,
and per-call retry on transient network errors.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = structlog.get_logger(__name__)

_MCP_PROTOCOL_VERSION = "2025-03-26"

# Only retry on transport-level failures; MCP-level errors (RuntimeError) are not retried.
_RETRYABLE = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


def _parse_sse_body(text: str) -> Any:
    """Extract the last JSON-RPC payload from an SSE stream body.

    MCP Streamable HTTP servers may respond with Content-Type: text/event-stream.
    Each event is formatted as 'data: <json>\\n\\n'.
    """
    parsed = None
    for chunk in text.split("\n\n"):
        for line in chunk.splitlines():
            if line.startswith("data: "):
                try:
                    parsed = json.loads(line[6:])
                except json.JSONDecodeError:
                    pass
    return parsed


class BaseMCPClient:
    """MCP Streamable HTTP transport base.

    Subclasses add domain-specific tool wrappers on top of the shared
    session lifecycle and _call_tool primitive.
    """

    def __init__(self, base_url: str, auth_token: str | None = None, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._rpc_counter = itertools.count(1)

    async def connect(self) -> None:
        """Open the HTTP session and perform the MCP initialize handshake."""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _MCP_PROTOCOL_VERSION,
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
        )

        init_request = {
            "jsonrpc": "2.0",
            "id": next(self._rpc_counter),
            "method": "initialize",
            "params": {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "vmware-ai-ops-agent", "version": "1.0.0"},
            },
        }
        response = await self._client.post("/mcp", json=init_request)
        response.raise_for_status()
        result = self._decode_response(response)

        self._session_id = response.headers.get("mcp-session-id")

        notify_headers: dict[str, str] = {}
        if self._session_id:
            notify_headers["mcp-session-id"] = self._session_id

        await self._client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers=notify_headers,
        )

        server_name = (result or {}).get("result", {}).get("serverInfo", {}).get("name", "unknown")
        logger.info(f"{self.__class__.__name__} connected", server_name=server_name)

    async def disconnect(self) -> None:
        """Close the underlying HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
        self._session_id = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.disconnect()

    def _decode_response(self, response: httpx.Response) -> Any:
        """Parse a JSON or SSE-framed MCP response."""
        if "text/event-stream" in response.headers.get("content-type", ""):
            return _parse_sse_body(response.text)
        return response.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_RETRYABLE),
        reraise=True,
    )
    async def _call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call an MCP tool, retrying on transient transport errors."""
        if not self._client:
            raise RuntimeError(f"{self.__class__.__name__} not connected. Call connect() first.")

        request = {
            "jsonrpc": "2.0",
            "id": next(self._rpc_counter),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        }

        headers: dict[str, str] = {}
        if self._session_id:
            headers["mcp-session-id"] = self._session_id

        response = await self._client.post("/mcp", json=request, headers=headers)
        response.raise_for_status()
        result = self._decode_response(response)

        if result and "error" in result:
            raise RuntimeError(f"MCP tool error: {result['error'].get('message', 'Unknown error')}")

        content = (result or {}).get("result", {}).get("content", [])
        if not content:
            return {}

        for block in content:
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except (json.JSONDecodeError, KeyError):
                    return {"raw_text": block.get("text", "")}
        return {}
