"""
LangGraph definition for the VMware AI Ops Agent.

Flow: collect → correlate → enrich (KB + capacity) → analyze → remediate
"""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from .analysis.models import AnalysisResult
from .collectors.models import InfrastructureState
from .correlation.engine import CorrelationResult
from .mcp_clients.ariaops import AriaOpsMCPClient
from .mcp_clients.entrag import EntragMCPClient


class AgentState(TypedDict):
    """State for the AI Ops Agent graph."""

    infrastructure_state: InfrastructureState | None
    correlation_result: CorrelationResult | None
    analysis_result: AnalysisResult | None
    kb_results: list[dict] | None
    search_results: list[dict] | None
    capacity_data: list[dict] | None
    remediation_status: dict[str, Any] | None
    errors: list[str]


def create_agent_graph(
    collector_func: Any,
    correlation_engine: Any,
    knowledge_base: Any,
    llm_engine: Any,
    remediator_func: Any,
    entrag_client: EntragMCPClient | None = None,
    ariaops_client: AriaOpsMCPClient | None = None,
):
    """
    Creates the LangGraph state machine.

    The graph now uses MCP clients for data enrichment:
    - entrag_client: KB article retrieval via EntRAG MCP server
    - ariaops_client: Capacity forecasting via AriaOps MCP server
    """

    # --- Nodes ---

    async def collect_node(state: AgentState) -> dict:
        try:
            infra_state = await collector_func()
            return {"infrastructure_state": infra_state}
        except Exception as e:
            return {"errors": state.get("errors", []) + [f"Collection failed: {str(e)}"]}

    def correlate_node(state: AgentState) -> dict:
        if not state.get("infrastructure_state"):
            return {}

        try:
            result = correlation_engine.correlate(state["infrastructure_state"])
            return {"correlation_result": result}
        except Exception as e:
            return {"errors": state.get("errors", []) + [f"Correlation failed: {str(e)}"]}

    async def enrich_node(state: AgentState) -> dict:
        """Parallel enrichment: KB search via EntRAG + capacity forecast via AriaOps."""
        correlation_result = state.get("correlation_result")
        issues = correlation_result.issues if correlation_result else []
        if not issues:
            return {}

        # Build query from correlated issues
        primary_issue = issues[0]
        query_parts = []
        try:
            pattern = getattr(primary_issue, "pattern", None)
            if pattern is not None:
                pattern_name = getattr(pattern, "name", None)
                if pattern_name:
                    query_parts.append(str(pattern_name))
        except (AttributeError, TypeError):
            pass

        description = getattr(primary_issue, "description", None)
        if description:
            query_parts.append(str(description))

        query = " ".join(query_parts) if query_parts else "VMware infrastructure issue"

        # Run enrichment tasks in parallel
        kb_hits: list[dict] = []
        web_hits: list[dict] = []
        capacity_data: list[dict] = []

        async def fetch_kb():
            """Fetch KB articles from EntRAG MCP or fall back to local KB."""
            nonlocal kb_hits, web_hits
            try:
                if entrag_client:
                    web_hits = await entrag_client.search(query)
                if knowledge_base:
                    similar = await knowledge_base.search_similar(query)
                    kb_hits = [h.model_dump() for h in similar]
            except Exception:
                # Don't fail the whole node, just log
                kb_hits = []
                web_hits = []

        async def fetch_capacity():
            """Fetch capacity forecasts for affected resources."""
            nonlocal capacity_data
            if not ariaops_client:
                return
            try:
                # Get capacity for resources mentioned in issues
                affected_resources = set()
                for issue in issues[:5]:  # Limit to top 5 issues
                    resources = getattr(issue, "affected_resources", [])
                    for res in resources:
                        res_id = getattr(res, "id", None) or (res if isinstance(res, str) else None)
                        if res_id:
                            affected_resources.add(res_id)

                for resource_id in list(affected_resources)[:3]:
                    try:
                        cap = await ariaops_client.get_capacity_remaining(resource_id)
                        capacity_data.append(cap)
                    except Exception:
                        pass
            except Exception:
                capacity_data = []

        await asyncio.gather(fetch_kb(), fetch_capacity())

        return {
            "kb_results": kb_hits,
            "search_results": web_hits,
            "capacity_data": capacity_data,
        }

    async def analyze_node(state: AgentState) -> dict:
        if not state.get("infrastructure_state"):
            return {}

        # Format context from KB and search results
        context_parts = []

        if state.get("kb_results"):
            context_parts.append("### Similar Past Incidents:")
            for hit in state["kb_results"]:
                meta = hit.get("metadata", {})
                summary = meta.get("summary") or hit.get("content", "No content")
                context_parts.append(f"- {summary} (Score: {hit.get('similarity_score', 0):.2f})")
                root_cause = meta.get("root_cause", "")
                if root_cause:
                    context_parts.append(f"  Root Cause: {root_cause}")

        if state.get("search_results"):
            context_parts.append("\n### Knowledge Base Articles:")
            for hit in state["search_results"]:
                title = hit.get("title", "Untitled")
                link = hit.get("link", "")
                section = hit.get("section_type", "")
                context_parts.append(f"- [{title}]({link})")
                snippet = hit.get("snippet", "")[:300]
                if snippet:
                    context_parts.append(f"  {snippet}...")
                if section:
                    context_parts.append(f"  Section: {section}")

        if state.get("capacity_data"):
            context_parts.append("\n### Capacity Analysis:")
            for cap in state["capacity_data"]:
                if isinstance(cap, dict):
                    resource_name = cap.get("resource_name", cap.get("resourceName", "Unknown"))
                    remaining = cap.get("remaining_capacity", cap.get("remainingCapacity", "N/A"))
                    time_remaining = cap.get("time_remaining", cap.get("timeRemaining", "N/A"))
                    context_parts.append(
                        f"- {resource_name}: {remaining}% remaining, "
                        f"~{time_remaining} days until exhaustion"
                    )

        context_str = "\n".join(context_parts)

        try:
            analysis = await llm_engine.analyze_infrastructure(
                state["infrastructure_state"],
                context=context_str,
            )
            return {"analysis_result": analysis}
        except Exception as e:
            return {"errors": state.get("errors", []) + [f"Analysis failed: {str(e)}"]}

    async def remediate_node(state: AgentState) -> dict:
        analysis = state.get("analysis_result")
        if not analysis:
            return {}

        try:
            result = await remediator_func(analysis)
            return {"remediation_status": {"executed": True, "details": str(result)}}
        except Exception as e:
            return {"errors": state.get("errors", []) + [f"Remediation failed: {str(e)}"]}

    # --- Conditional Logic ---

    def should_enrich(state: AgentState) -> str:
        if not state.get("infrastructure_state"):
            return END
        correlation_result = state.get("correlation_result")
        if correlation_result and correlation_result.issues:
            return "enrich"
        return END

    def should_remediate(state: AgentState) -> str:
        if state.get("errors"):
            return END
        analysis = state.get("analysis_result")
        if analysis and analysis.remediation_plan and analysis.remediation_plan.auto_executable:
            return "remediate"
        return END

    # --- Graph Construction ---

    workflow = StateGraph(AgentState)

    workflow.add_node("collect", collect_node)
    workflow.add_node("correlate", correlate_node)
    workflow.add_node("enrich", enrich_node)
    workflow.add_node("analyze", analyze_node)
    workflow.add_node("remediate", remediate_node)

    workflow.set_entry_point("collect")

    workflow.add_edge("collect", "correlate")
    workflow.add_conditional_edges("correlate", should_enrich)
    workflow.add_edge("enrich", "analyze")
    workflow.add_conditional_edges("analyze", should_remediate)
    workflow.add_edge("remediate", END)

    return workflow.compile()
