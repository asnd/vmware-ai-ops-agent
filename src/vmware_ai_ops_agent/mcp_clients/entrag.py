"""
MCP client for EntRAG (VMware/Broadcom KB RAG) server.

Replaces the DuckDuckGo-based BroadcomKBSearch with production-grade
RAG retrieval: hybrid search, section-aware chunking, intent-boosted
reranking, and metadata-rich citations.

Inherits session lifecycle, SSE response handling, and transport-level
retry from BaseMCPClient.
"""

from __future__ import annotations

from typing import Any

import structlog

from .base import BaseMCPClient

logger = structlog.get_logger(__name__)


class EntragMCPClient(BaseMCPClient):
    """MCP client adapter for EntRAG KB retrieval server.

    Communicates via MCP Streamable HTTP transport. Inherits session
    lifecycle, SSE/JSON response parsing, and retry from BaseMCPClient.
    """

    def __init__(
        self,
        base_url: str,
        auth_token: str | None = None,
        timeout: float = 60.0,
    ):
        super().__init__(base_url=base_url, auth_token=auth_token, timeout=timeout)

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

        if isinstance(result, dict):
            if "results" in result:
                return result["results"]
            if "chunks" in result:
                return result["chunks"]
            if "raw_text" in result:
                return [{"content": result["raw_text"], "title": "KB Result", "score": 1.0}]
            if "content" in result or "text" in result:
                return [result]
        elif isinstance(result, list):
            return result

        return []

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        """Search KB articles — compatible interface with BroadcomKBSearch.

        Returns list of dicts with 'title', 'link', 'snippet' keys.
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
