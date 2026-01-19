"""
LangGraph definition for the VMware AI Ops Agent.
"""

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from .analysis.models import AnalysisResult
from .collectors.models import InfrastructureState
from .correlation.engine import CorrelationResult
from .tools.search import BroadcomKBSearch

class AgentState(TypedDict):
    """State for the AI Ops Agent graph."""
    infrastructure_state: InfrastructureState | None
    correlation_result: CorrelationResult | None
    analysis_result: AnalysisResult | None
    kb_results: list[dict] | None
    search_results: list[dict] | None
    remediation_status: dict[str, Any] | None
    errors: list[str]

def create_agent_graph(
    collector_func: Any,
    correlation_engine: Any,
    knowledge_base: Any,
    llm_engine: Any,
    remediator_func: Any,
    search_tool: BroadcomKBSearch
):
    """
    Creates the LangGraph state machine.
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

    async def search_node(state: AgentState) -> dict:
        correlation_result = state.get("correlation_result")
        issues = correlation_result.issues if correlation_result else []
        if not issues:
            return {}

        # Search for the first/most critical issue
        primary_issue = issues[0]

        # Build query safely with defensive attribute access
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
        
        kb_hits = []
        if knowledge_base:
            kb_hits = await knowledge_base.search_similar(query)
            # Convert SimilarityResult to dict for state
            kb_hits = [h.model_dump() for h in kb_hits]

        web_hits = search_tool.search(query)
        
        return {"kb_results": kb_hits, "search_results": web_hits}

    async def analyze_node(state: AgentState) -> dict:
        if not state.get("infrastructure_state"):
            return {}
            
        # Format context from KB and search results
        context_parts = []
        
        if state.get("kb_results"):
            context_parts.append("### Similar Past Incidents:")
            for hit in state["kb_results"]:
                context_parts.append(f"- {hit.get('summary', 'No summary')} (Score: {hit.get('similarity_score', 0):.2f})")
                if hit.get('root_cause'):
                    context_parts.append(f"  Root Cause: {hit.get('root_cause')}")

        if state.get("search_results"):
            context_parts.append("\n### Knowledge Base Articles:")
            for hit in state["search_results"]:
                context_parts.append(f"- [{hit.get('title')}]({hit.get('link')})")
                context_parts.append(f"  Snippet: {hit.get('snippet', '')[:200]}...")
        
        context_str = "\n".join(context_parts)

        try:
            analysis = await llm_engine.analyze_infrastructure(
                state["infrastructure_state"],
                context=context_str
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

    def should_analyze(state: AgentState) -> str:
        if state.get("errors"):
            return END
        correlation_result = state.get("correlation_result")
        if correlation_result and correlation_result.issues:
            return "search"
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
    workflow.add_node("search", search_node)
    workflow.add_node("analyze", analyze_node)
    workflow.add_node("remediate", remediate_node)

    workflow.set_entry_point("collect")
    
    workflow.add_edge("collect", "correlate")
    workflow.add_conditional_edges("correlate", should_analyze)
    workflow.add_edge("search", "analyze")
    workflow.add_conditional_edges("analyze", should_remediate)
    workflow.add_edge("remediate", END)

    return workflow.compile()
