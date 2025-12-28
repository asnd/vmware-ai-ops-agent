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
from .collectors.vrops import VROpsCollector
from .config import Settings
from .correlation.engine import CorrelatedIssue, CorrelationEngine, CorrelationResult

logger = structlog.get_logger(__name__)

ANALYSIS_CYCLES = Counter(
    "vmware_ai_agent_analysis_cycles_total", "Total analysis cycles", ["status"]
)
ISSUES_DETECTED = Counter(
    "vmware_ai_agent_issues_detected_total", "Issues detected", ["severity"]
)
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
    """Main AI Ops Agent for VMware infrastructure."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = AgentState()
        self.correlation_engine = CorrelationEngine()
        self.llm_engine = LLMAnalysisEngine(settings.llm)
        self.knowledge_base = KnowledgeBase(settings.vector_db, settings.knowledge_base)
        self._scheduler: AsyncIOScheduler | None = None
        self._on_issue_detected: Callable[[CorrelatedIssue], None] | None = None
        self._on_analysis_complete: Callable[[AnalysisResult], None] | None = None
        self._approval_callback: Callable[[Any], bool] | None = None

    async def start(self) -> None:
        logger.info("Starting VMware AI Ops Agent")
        await self.knowledge_base.initialize()

        if self.settings.metrics.enabled:
            start_http_server(self.settings.metrics.port)
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
        logger.info("Agent stopped")

    async def _run_cycle(self) -> None:
        cycle_start = datetime.utcnow()
        logger.info("Starting analysis cycle", cycle=self.state.total_cycles + 1)

        try:
            state = await self._collect_infrastructure_state()
            correlation_result = self.correlation_engine.correlate(state)
            self.state.last_correlation = correlation_result

            for issue in correlation_result.issues:
                ISSUES_DETECTED.labels(severity=issue.severity.value).inc()
                self.state.issues_detected += 1

            if correlation_result.issues:
                analysis = await self.llm_engine.analyze_infrastructure(state)
                self.state.last_analysis = analysis
                await self._handle_analysis_results(analysis, correlation_result)
                await self.knowledge_base.record_analysis(analysis)

                if self.settings.agent.auto_remediate.enabled:
                    await self._auto_remediate(analysis)

            for resource in state.resources:
                RESOURCE_HEALTH.labels(
                    resource_name=resource.resource.name,
                    resource_kind=resource.resource.kind.value,
                ).set(resource.health_score)

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
        state = InfrastructureState()

        async def collect_vrops():
            try:
                async with VROpsCollector(self.settings.vrops) as vrops:
                    return await vrops.collect_all()
            except Exception as e:
                logger.error("vROps collection failed", error=str(e))
                return [], [], [], []

        async def collect_vrli():
            try:
                async with VRLICollector(self.settings.vrli) as vrli:
                    return await vrli.collect_all()
            except Exception as e:
                logger.error("vRLI collection failed", error=str(e))
                return [], []

        vrops_result, vrli_result = await asyncio.gather(collect_vrops(), collect_vrli())

        resources, alerts, recommendations, anomalies = vrops_result
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
            logs=len(state.recent_logs),
        )
        return state

    async def _handle_analysis_results(
        self, analysis: AnalysisResult, correlation: CorrelationResult
    ) -> None:
        if self._on_analysis_complete:
            self._on_analysis_complete(analysis)

        for issue in correlation.issues:
            if self._on_issue_detected:
                self._on_issue_detected(issue)

        if analysis.urgency in (Urgency.CRITICAL, Urgency.HIGH):
            try:
                async with NotificationService(self.settings.notifications) as notifications:
                    await notifications.notify_analysis(analysis)
            except Exception as e:
                logger.error("Notification failed", error=str(e))

    async def _auto_remediate(self, analysis: AnalysisResult) -> None:
        if not analysis.remediation_plan or not analysis.remediation_plan.auto_executable:
            return

        logger.info("Executing auto-remediation", plan_id=analysis.remediation_plan.id)

        try:
            async with VCenterClient(self.settings.vcenter) as vcenter:
                async with NotificationService(self.settings.notifications) as notifications:
                    executor = ActionExecutor(
                        self.settings.agent, vcenter=vcenter, notifications=notifications
                    )
                    result = await executor.execute_plan(
                        analysis.remediation_plan,
                        approval_callback=self._approval_callback,
                    )
                    self.state.actions_executed += sum(
                        1 for r in result.action_results if r.success
                    )
        except Exception as e:
            logger.error("Auto-remediation failed", error=str(e))

    async def analyze_now(self) -> AnalysisResult | None:
        logger.info("Triggering immediate analysis")
        try:
            state = await self._collect_infrastructure_state()
            correlation = self.correlation_engine.correlate(state)
            analysis = await self.llm_engine.analyze_infrastructure(state)
            self.state.last_analysis = analysis
            self.state.last_correlation = correlation
            return analysis
        except Exception as e:
            logger.error("Immediate analysis failed", error=str(e))
            return None

    def get_status(self) -> dict[str, Any]:
        return {
            "running": self.state.running,
            "total_cycles": self.state.total_cycles,
            "last_cycle_at": (
                self.state.last_cycle_at.isoformat()
                if self.state.last_cycle_at
                else None
            ),
            "issues_detected": self.state.issues_detected,
            "actions_executed": self.state.actions_executed,
            "last_analysis_urgency": (
                self.state.last_analysis.urgency.value
                if self.state.last_analysis
                else None
            ),
            "knowledge_base": self.knowledge_base.get_statistics(),
        }

    def get_last_analysis(self) -> AnalysisResult | None:
        return self.state.last_analysis
