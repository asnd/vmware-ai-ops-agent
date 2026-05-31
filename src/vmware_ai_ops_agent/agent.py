"""
Main VMware AI Ops Agent orchestrator.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from .actions.executor import ActionExecutor
from .actions.notifications import NotificationService
from .actions.vcenter import VCenterClient
from .analysis.knowledge_base import KnowledgeBase
from .analysis.llm_engine import LLMAnalysisEngine
from .analysis.models import AnalysisResult, Urgency
from .collectors.models import InfrastructureState
from .collectors.vrli import VRLICollector
from .config import Settings
from .correlation.engine import CorrelatedIssue, CorrelationEngine, CorrelationResult
from .correlation.patterns import KNOWN_PATTERNS, load_custom_patterns
from .graph import create_agent_graph
from .mcp_clients.ariaops import AriaOpsMCPClient
from .mcp_clients.entrag import EntragMCPClient

logger = structlog.get_logger(__name__)

ANALYSIS_CYCLES = Counter(
    "vmware_ai_agent_analysis_cycles_total", "Total analysis cycles", ["status"]
)
ISSUES_DETECTED = Counter("vmware_ai_agent_issues_detected_total", "Issues detected", ["severity"])
CYCLE_DURATION = Histogram(
    "vmware_ai_agent_cycle_duration_seconds",
    "Cycle duration",
    buckets=[5, 10, 30, 60, 120, 300],
)
RESOURCE_HEALTH = Gauge(
    "vmware_ai_agent_resource_health", "Resource health", ["resource_name", "resource_kind"]
)


@dataclass
class AgentState:
    running: bool = False
    last_cycle_at: datetime | None = None
    last_cycle_duration: float = 0.0
    total_cycles: int = 0
    issues_detected: int = 0
    actions_executed: int = 0
    last_analysis: AnalysisResult | None = None
    last_correlation: CorrelationResult | None = None
    errors: list[str] = field(default_factory=list)


class VMwareAIOpsAgent:
    """Main AI Ops Agent for VMware infrastructure.

    Uses MCP clients for data collection (AriaOps) and knowledge
    retrieval (EntRAG), replacing direct API clients.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = AgentState()
        self.correlation_engine = CorrelationEngine(patterns=self._load_patterns())
        self.llm_engine = LLMAnalysisEngine(settings.llm)
        self._metrics_server = None

        self.knowledge_base = KnowledgeBase(
            settings.vector_db,
            settings.knowledge_base,
            api_key=settings.llm.api_key.get_secret_value(),
            signing_secret=settings.knowledge_base.signing_secret.get_secret_value(),
        )

        # MCP Clients
        self.ariaops_client: AriaOpsMCPClient | None = None
        self.entrag_client: EntragMCPClient | None = None

        if settings.ariaops_mcp.enabled:
            self.ariaops_client = AriaOpsMCPClient(
                base_url=settings.ariaops_mcp.url,
                auth_token=settings.ariaops_mcp.auth_token.get_secret_value() or None,
                timeout=settings.ariaops_mcp.timeout,
            )

        if settings.entrag_mcp.enabled:
            self.entrag_client = EntragMCPClient(
                base_url=settings.entrag_mcp.url,
                auth_token=settings.entrag_mcp.auth_token.get_secret_value() or None,
                timeout=settings.entrag_mcp.timeout,
            )

        # Executor is created once so rate-limit and dedup state survive across cycles.
        # vcenter/notifications are injected per-call inside _auto_remediate.
        self._executor: ActionExecutor | None = None

        self.graph = create_agent_graph(
            collector_func=self._collect_infrastructure_state,
            correlation_engine=self.correlation_engine,
            knowledge_base=self.knowledge_base,
            llm_engine=self.llm_engine,
            remediator_func=self._auto_remediate_wrapper,
            entrag_client=self.entrag_client,
            ariaops_client=self.ariaops_client,
        )

        self._scheduler: AsyncIOScheduler | None = None
        self._on_issue_detected: Callable[[CorrelatedIssue], None] | None = None
        self._on_analysis_complete: Callable[[AnalysisResult], None] | None = None
        self._approval_callback: Callable[[Any], bool] | None = None

    def _load_patterns(self) -> list:
        """Merge built-in patterns with any site-specific ones from config."""
        patterns = list(KNOWN_PATTERNS)
        custom_file = self.settings.correlation.custom_patterns_file
        if custom_file:
            try:
                custom = load_custom_patterns(custom_file)
                patterns.extend(custom)
                logger.info("Custom correlation patterns loaded", count=len(custom))
            except Exception as e:
                logger.error("Failed to load custom patterns", error=str(e))
        return patterns

    async def start(self) -> None:
        logger.info("Starting VMware AI Ops Agent")
        await self.knowledge_base.initialize()

        # Connect MCP clients
        if self.ariaops_client:
            try:
                await self.ariaops_client.connect()
                logger.info("AriaOps MCP client connected")
            except Exception as e:
                logger.error("Failed to connect AriaOps MCP client", error=str(e))
                self.ariaops_client = None

        if self.entrag_client:
            try:
                await self.entrag_client.connect()
                logger.info("EntRAG MCP client connected")
            except Exception as e:
                logger.error("Failed to connect EntRAG MCP client", error=str(e))
                self.entrag_client = None

        # Create executor after MCP clients are resolved so ariaops_client is final.
        self._executor = ActionExecutor(
            self.settings.agent,
            ariaops_client=self.ariaops_client,
        )

        if self.settings.metrics.enabled:
            start_http_server(self.settings.metrics.port)
            self._metrics_server = True
            logger.info("Metrics server started", port=self.settings.metrics.port)

        self._scheduler = AsyncIOScheduler()
        self._scheduler.add_job(
            self._run_cycle,
            "interval",
            seconds=self.settings.agent.cycle_interval,
            id="main_cycle",
            max_instances=1,
        )

        self.state.running = True
        self._scheduler.start()

        logger.info("Agent started", cycle_interval=self.settings.agent.cycle_interval)
        await self._run_cycle()

    async def stop(self) -> None:
        logger.info("Stopping VMware AI Ops Agent")
        self.state.running = False
        if self._scheduler:
            self._scheduler.shutdown()

        # Disconnect MCP clients
        if self.ariaops_client:
            await self.ariaops_client.disconnect()
            logger.info("AriaOps MCP client disconnected")
        if self.entrag_client:
            await self.entrag_client.disconnect()
            logger.info("EntRAG MCP client disconnected")

        # Flush any pending KB documents before shutdown
        await self.knowledge_base.flush()
        if self._metrics_server:
            logger.info("Metrics server will terminate with process")
        logger.info("Agent stopped")

    def _initial_graph_state(self) -> dict[str, Any]:
        """Return a fresh initial state dict for graph invocations."""
        return {
            "infrastructure_state": None,
            "correlation_result": None,
            "analysis_result": None,
            "kb_results": None,
            "search_results": None,
            "capacity_data": None,
            "remediation_status": None,
            "errors": [],
        }

    async def _run_cycle(self) -> None:
        cycle_start = datetime.utcnow()
        logger.info("Starting analysis cycle", cycle=self.state.total_cycles + 1)

        try:
            # Execute the LangGraph workflow
            graph_result = await self.graph.ainvoke(self._initial_graph_state())

            # Update internal state and metrics from graph result
            if graph_result.get("infrastructure_state"):
                state = graph_result["infrastructure_state"]
                for resource in state.resources:
                    RESOURCE_HEALTH.labels(
                        resource_name=resource.resource.name,
                        resource_kind=resource.resource.kind.value,
                    ).set(resource.health_score)

            if graph_result.get("correlation_result"):
                correlation_result = graph_result["correlation_result"]
                self.state.last_correlation = correlation_result
                for issue in correlation_result.issues:
                    ISSUES_DETECTED.labels(severity=issue.severity.value).inc()
                    self.state.issues_detected += 1
                    if self._on_issue_detected:
                        self._on_issue_detected(issue)

            if graph_result.get("analysis_result"):
                analysis = graph_result["analysis_result"]
                self.state.last_analysis = analysis
                await self.knowledge_base.record_analysis(analysis)
                await self._handle_analysis_results(analysis)

            if graph_result.get("remediation_status"):
                if graph_result["remediation_status"].get("executed"):
                    self.state.actions_executed += 1

            if graph_result.get("errors"):
                for err in graph_result["errors"]:
                    logger.error("Graph execution error", error=err)
                    self.state.errors.append(f"{datetime.utcnow().isoformat()}: {err}")
                ANALYSIS_CYCLES.labels(status="partial_error").inc()
            else:
                ANALYSIS_CYCLES.labels(status="success").inc()

        except Exception as e:
            logger.error("Analysis cycle failed", error=str(e))
            ANALYSIS_CYCLES.labels(status="error").inc()
            self.state.errors.append(f"{datetime.utcnow().isoformat()}: {str(e)}")
            self.state.errors = self.state.errors[-100:]

        finally:
            cycle_end = datetime.utcnow()
            duration = (cycle_end - cycle_start).total_seconds()
            self.state.last_cycle_at = cycle_end
            self.state.last_cycle_duration = duration
            self.state.total_cycles += 1
            CYCLE_DURATION.observe(duration)
            logger.info("Analysis cycle complete", duration_seconds=duration)

    async def _collect_infrastructure_state(self) -> InfrastructureState:
        """Collect infrastructure state via MCP client (preferred) or direct collector."""
        state = InfrastructureState()

        async def collect_ariaops():
            """Collect from AriaOps MCP server."""
            if self.ariaops_client:
                try:
                    return await self.ariaops_client.collect_all()
                except Exception as e:
                    logger.error("AriaOps MCP collection failed", error=str(e))
            # Fallback: return empty
            return [], [], [], []

        async def collect_vrli():
            try:
                async with VRLICollector(self.settings.vrli) as vrli:
                    return await vrli.collect_all()
            except Exception as e:
                logger.error("vRLI collection failed", error=str(e))
                return [], []

        try:
            ariaops_result, vrli_result = await asyncio.wait_for(
                asyncio.gather(collect_ariaops(), collect_vrli()),
                timeout=120.0,
            )
        except TimeoutError:
            logger.error("Infrastructure collection timed out after 120s")
            ariaops_result = ([], [], [], [])
            vrli_result = ([], [])

        resources, alerts, recommendations, anomalies = ariaops_result
        logs, log_anomalies = vrli_result

        state.resources = resources
        state.alerts = alerts
        state.recommendations = recommendations
        state.anomalies.extend(anomalies)
        state.recent_logs = logs
        state.anomalies.extend(log_anomalies)

        logger.info(
            "Infrastructure state collected",
            resources=len(state.resources),
            alerts=len(state.alerts),
            logs=len(state.recent_logs),
        )
        return state

    async def _handle_analysis_results(self, analysis: AnalysisResult) -> None:
        if self._on_analysis_complete:
            self._on_analysis_complete(analysis)

        if analysis.urgency in (Urgency.CRITICAL, Urgency.HIGH):
            try:
                async with NotificationService(self.settings.notifications) as notifications:
                    await notifications.notify_analysis(analysis)
            except Exception as e:
                logger.error("Notification failed", error=str(e))

    async def _auto_remediate_wrapper(self, analysis: AnalysisResult) -> dict[str, Any]:
        """Wrapper for auto-remediation to return results to graph."""
        if not self.settings.agent.auto_remediate.enabled:
            return {"status": "disabled"}

        result = await self._auto_remediate(analysis)
        return result or {"status": "no_action"}

    async def _auto_remediate(self, analysis: AnalysisResult) -> dict[str, Any] | None:
        if not analysis.remediation_plan or not analysis.remediation_plan.auto_executable:
            return None

        if self._executor is None:
            logger.error("Executor not initialised — call start() before remediating")
            return None

        logger.info("Executing auto-remediation", plan_id=analysis.remediation_plan.id)

        try:
            async with VCenterClient(self.settings.vcenter) as vcenter:
                async with NotificationService(self.settings.notifications) as notifications:
                    # Inject per-call clients; the executor's rate-limit/dedup state persists.
                    self._executor.vcenter = vcenter
                    self._executor.notifications = notifications
                    try:
                        result = await self._executor.execute_plan(
                            analysis.remediation_plan,
                            approval_callback=self._approval_callback,
                        )
                    finally:
                        self._executor.vcenter = None
                        self._executor.notifications = None

                    success_count = sum(1 for r in result.action_results if r.success)

                    return {
                        "executed": True,
                        "success_count": success_count,
                        "plan_id": analysis.remediation_plan.id,
                        "results": [r.status.value for r in result.action_results],
                    }
        except Exception as e:
            logger.error("Auto-remediation failed", error=str(e))
            raise

    async def analyze_now(self) -> AnalysisResult | None:
        """Trigger immediate analysis outside of scheduled cycle."""
        logger.info("Triggering immediate analysis")
        try:
            result = await self.graph.ainvoke(self._initial_graph_state())

            analysis = result.get("analysis_result")

            if analysis:
                self.state.last_analysis = analysis
                await self.knowledge_base.record_analysis(analysis)

            if result.get("errors"):
                for err in result["errors"]:
                    logger.error("Immediate analysis error", error=err)

            return analysis
        except Exception as e:
            logger.error("Immediate analysis failed", error=str(e))
            return None

    def get_status(self) -> dict[str, Any]:
        return {
            "running": self.state.running,
            "total_cycles": self.state.total_cycles,
            "last_cycle_at": (
                self.state.last_cycle_at.isoformat() if self.state.last_cycle_at else None
            ),
            "issues_detected": self.state.issues_detected,
            "actions_executed": self.state.actions_executed,
            "last_analysis_urgency": (
                self.state.last_analysis.urgency.value if self.state.last_analysis else None
            ),
            "knowledge_base": self.knowledge_base.get_statistics(),
            "mcp_clients": {
                "ariaops": "connected" if self.ariaops_client else "disabled",
                "entrag": "connected" if self.entrag_client else "disabled",
            },
        }

    def get_last_analysis(self) -> AnalysisResult | None:
        return self.state.last_analysis
