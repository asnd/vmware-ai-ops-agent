"""
MCP client for EntRAG (VMware/Broadcom KB RAG) server.

Replaces the DuckDuckGo-based BroadcomKBSearch with production-grade
RAG retrieval: hybrid search, section-aware chunking, intent-boosted
reranking, and metadata-rich citations.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


class EntragMCPClient:
    """MCP client adapter for EntRAG KB retrieval server.

    Communicates via MCP Streamable HTTP transport.
    """

    def __init__(
        self,
        base_url: str,
        auth_token: str | None = None,
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None

    async def connect(self) -> None:
        """Initialize HTTP client and establish MCP session."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
        )

        # Initialize MCP session
        init_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "vmware-ai-ops-agent", "version": "1.0.0"},
            },
        }
        response = await self._client.post("/mcp", json=init_request)
        response.raise_for_status()
        result = response.json()

        self._session_id = response.headers.get("mcp-session-id")

        # Send initialized notification
        initialized_notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        notify_headers = {}
        if self._session_id:
            notify_headers["mcp-session-id"] = self._session_id
        await self._client.post("/mcp", json=initialized_notification, headers=notify_headers)

        logger.info(
            "EntRAG MCP client connected",
            server_name=result.get("result", {}).get("serverInfo", {}).get("name", "unknown"),
        )

    async def disconnect(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
        self._session_id = None

    async def __aenter__(self) -> EntragMCPClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.disconnect()

    async def _call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call an MCP tool and return the parsed result."""
        if not self._client:
            raise RuntimeError("Client not connected. Call connect() first.")

        request = {
            "jsonrpc": "2.0",
            "id": id(asyncio.current_task()) or 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        }

        headers = {}
        if self._session_id:
            headers["mcp-session-id"] = self._session_id

        response = await self._client.post("/mcp", json=request, headers=headers)
        response.raise_for_status()
        result = response.json()

        if "error" in result:
            error = result["error"]
            raise RuntimeError(f"MCP tool error: {error.get('message', 'Unknown error')}")

        content = result.get("result", {}).get("content", [])
        if not content:
            return {}

        for block in content:
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except (json.JSONDecodeError, KeyError):
                    return {"raw_text": block.get("text", "")}
        return {}

    # --- RAG Query Tool ---

    async def search_kb(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Search VMware/Broadcom KB articles via RAG.

        Returns structured results with citations including:
        - article_number, title, url
        - section_type (symptom, cause, resolution)
        - relevance_score
        - content snippet
        """
        result = await self._call_tool("rag_query", {"query": query, "top_k": top_k})

        # Handle different response shapes
        if isinstance(result, dict):
            if "results" in result:
                return result["results"]
            if "chunks" in result:
                return result["chunks"]
            if "raw_text" in result:
                # Fallback: wrap raw text as single result
                return [{"content": result["raw_text"], "title": "KB Result", "score": 1.0}]
            # Return as single-item list if it looks like a result
            if "content" in result or "text" in result:
                return [result]
        elif isinstance(result, list):
            return result

        return []

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        """Search KB articles - compatible interface with BroadcomKBSearch.

        Returns list of dicts with 'title', 'link', 'snippet' keys
        for backward compatibility with the graph search node.
        """
        results = await self.search_kb(query, top_k=max_results)

        formatted: list[dict[str, str]] = []
        for r in results:
            formatted.append(
                {
                    "title": r.get("title", r.get("article_number", "KB Article")),
                    "link": r.get("url", r.get("link", "")),
                    "snippet": r.get("content", r.get("text", r.get("snippet", "")))[:500],
                    "section_type": r.get("section_type", ""),
                    "score": str(r.get("relevance_score", r.get("score", 0.0))),
                }
            )

        logger.info("EntRAG KB search performed", query=query[:80], results_found=len(formatted))
        return formatted

    # --- Ingestion Status ---

    async def get_ingestion_status(self) -> dict[str, Any]:
        """Get the status of the EntRAG knowledge base index."""
        return await self._call_tool("ingestion_status")

    # --- Scrape Status ---

    async def get_scrape_status(self) -> dict[str, Any]:
        """Get the status of the KB article scraper."""
        return await self._call_tool("scrape_status")
