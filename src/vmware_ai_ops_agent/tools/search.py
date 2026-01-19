"""
Broadcom Knowledge Base search tool.
"""

import structlog
from duckduckgo_search import DDGS

logger = structlog.get_logger(__name__)

class BroadcomKBSearch:
    """Tool for searching Broadcom/VMware Knowledge Base articles."""

    def __init__(self):
        self.ddgs = DDGS()

    def search(self, query: str, max_results: int = 3) -> list[dict[str, str]]:
        """
        Search for Broadcom KB articles.
        
        Args:
            query: The search query (e.g., error message or fault description).
            max_results: Maximum number of results to return.
            
        Returns:
            List of dictionaries containing 'title', 'link', and 'snippet'.
        """
        # Constrain search to broadcom.com/support/knowledgebase or similar
        # Broadcom's KB structure is a bit complex, but searching site:broadcom.com usually works
        # or specifically site:knowledge.broadcom.com if applicable.
        # "site:vmware.com" is also still very relevant for older KBs.
        
        search_query = f"{query} site:broadcom.com OR site:vmware.com"
        
        try:
            results = list(self.ddgs.text(search_query, max_results=max_results))
            formatted_results = []
            for r in results:
                formatted_results.append({
                    "title": r.get("title", ""),
                    "link": r.get("href", ""),
                    "snippet": r.get("body", "")
                })
            logger.info("KB search performed", query=query, results_found=len(formatted_results))
            return formatted_results
        except Exception as e:
            logger.error("KB search failed", error=str(e))
            return []
