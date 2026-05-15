"""
Broadcom Knowledge Base search tool.

DEPRECATED: Use EntragMCPClient for production-grade RAG retrieval.
This module is retained only for backward compatibility and fallback.
"""

import structlog

logger = structlog.get_logger(__name__)


class BroadcomKBSearch:
    """Tool for searching Broadcom/VMware Knowledge Base articles.

    DEPRECATED: This uses DuckDuckGo which is fragile and ungrounded.
    Use EntragMCPClient instead for hybrid RAG with cited KB articles.
    """

    def __init__(self):
        self._ddgs = None

    def _get_ddgs(self):
        """Lazy-load DDGS to avoid import error when duckduckgo-search not installed."""
        if self._ddgs is None:
            try:
                from duckduckgo_search import DDGS

                self._ddgs = DDGS()
            except ImportError:
                logger.warning(
                    "duckduckgo-search not installed; BroadcomKBSearch is non-functional. "
                    "Use EntragMCPClient instead."
                )
                self._ddgs = None
        return self._ddgs

    def search(self, query: str, max_results: int = 3) -> list[dict[str, str]]:
        """
        Search for Broadcom KB articles via DuckDuckGo (deprecated).

        Args:
            query: The search query.
            max_results: Maximum number of results to return.

        Returns:
            List of dictionaries containing 'title', 'link', and 'snippet'.
        """
        ddgs = self._get_ddgs()
        if ddgs is None:
            return []

        search_query = f"{query} site:broadcom.com OR site:vmware.com"

        try:
            results = list(ddgs.text(search_query, max_results=max_results))
            formatted_results = []
            for r in results:
                formatted_results.append(
                    {
                        "title": r.get("title", ""),
                        "link": r.get("href", ""),
                        "snippet": r.get("body", ""),
                    }
                )
            logger.info("KB search performed", query=query, results_found=len(formatted_results))
            return formatted_results
        except Exception as e:
            logger.error("KB search failed", error=str(e))
            return []
