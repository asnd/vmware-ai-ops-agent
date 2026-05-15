"""
LLM-powered analysis engine for VMware infrastructure.
"""

import json
import time
import uuid
from typing import Any

import structlog
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from ..collectors.models import Alert, InfrastructureState, LogEntry, ResourceHealth
from ..config import LLMConfig
from ..utils.security import scrub_sensitive_data
from .models import (
    ActionType,
    AnalysisResult,
    PredictedFailure,
    RemediationPlan,
    RemediationStep,
    RootCauseAnalysis,
    Urgency,
)

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are an expert VMware infrastructure analyst.
Analyze vROps metrics and vRLI logs to:
1. Identify correlations and patterns
2. Detect potential failures
3. Provide root cause analysis
4. Recommend preventive actions

You have deep knowledge of VMware vSphere, ESXi, vCenter, storage, and networking.
Provide structured, actionable insights with confidence levels."""


def _safe_urgency(value: str) -> Urgency:
    """Safely convert string to Urgency enum."""
    if not value:
        return Urgency.MEDIUM
    normalized = value.lower().strip()
    try:
        return Urgency(normalized)
    except ValueError:
        # Map common variations
        mapping = {
            "high": Urgency.HIGH,
            "critical": Urgency.CRITICAL,
            "low": Urgency.LOW,
            "medium": Urgency.MEDIUM,
            "moderate": Urgency.MEDIUM,
            "severe": Urgency.CRITICAL,
            "urgent": Urgency.HIGH,
        }
        return mapping.get(normalized, Urgency.MEDIUM)


def _safe_action_type(value: str) -> ActionType:
    """Safely convert string to ActionType enum."""
    if not value:
        return ActionType.INVESTIGATE
    normalized = value.lower().strip().replace(" ", "_").replace("-", "_")
    try:
        return ActionType(normalized)
    except ValueError:
        return ActionType.INVESTIGATE


class LLMAnalysisEngine:
    """AI analysis engine using LLM for infrastructure analysis."""

    # Timeout for LLM requests in seconds
    LLM_TIMEOUT = 120

    def __init__(self, config: LLMConfig):
        self.config = config
        api_key = config.api_key.get_secret_value()
        if not api_key:
            logger.warning("No LLM API key provided, using placeholder")
            api_key = "not-configured"
        self.client = AsyncOpenAI(
            base_url=config.endpoint,
            api_key=api_key,
            timeout=self.LLM_TIMEOUT,
        )
        self.model = config.model

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def _chat_completion(self, system_prompt: str, user_prompt: str) -> tuple[str, int]:
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
            content = response.choices[0].message.content or ""
            tokens = response.usage.total_tokens if response.usage else 0
            return content, tokens
        except Exception as e:
            logger.error("LLM request failed", error=str(e))
            raise

    def _format_metrics(self, resources: list[ResourceHealth], max_resources: int = 20) -> str:
        sorted_resources = sorted(resources, key=lambda r: r.health_score)[:max_resources]
        lines = ["## Current Resource Health\n"]
        for resource in sorted_resources:
            lines.append(f"### {resource.resource.name} ({resource.resource.kind.value})")
            lines.append(f"- Health: {resource.health_state.value} ({resource.health_score:.1f}%)")
            lines.append(f"- Workload: {resource.workload_score:.1f}%")
            lines.append(f"- Anomaly Score: {resource.anomaly_score:.1f}%")
            lines.append("")
        return "\n".join(lines)

    def _format_alerts(self, alerts: list[Alert], max_alerts: int = 20) -> str:
        severity_order = {"CRITICAL": 0, "IMMEDIATE": 1, "WARNING": 2, "INFO": 3}
        sorted_alerts = sorted(alerts, key=lambda a: severity_order.get(a.severity.value, 4))[
            :max_alerts
        ]
        lines = ["## Active Alerts\n"]
        for alert in sorted_alerts:
            lines.append(f"### [{alert.severity.value}] {alert.name}")
            lines.append(f"- Resource: {alert.resource.name}")
            lines.append(f"- Description: {alert.description}")
            lines.append("")
        return "\n".join(lines)

    def _format_logs(self, logs: list[LogEntry], max_logs: int = 50) -> str:
        sorted_logs = sorted(logs, key=lambda log: log.timestamp, reverse=True)[:max_logs]
        lines = ["## Recent Logs\n"]
        for log in sorted_logs:
            timestamp = log.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"[{timestamp}] [{log.source}] {log.text[:300]}")
        return "\n".join(lines)

    async def analyze_infrastructure(
        self, state: InfrastructureState, context: str = ""
    ) -> AnalysisResult:
        start_time = time.time()
        total_tokens = 0

        metrics_text = scrub_sensitive_data(self._format_metrics(state.resources))
        alerts_text = scrub_sensitive_data(self._format_alerts(state.alerts))
        logs_text = scrub_sensitive_data(self._format_logs(state.recent_logs))

        context_section = ""
        if context:
            context_section = f"""
## Additional Context (Knowledge Base & Search Results)
{context}
"""

        user_prompt = f"""Analyze the following VMware infrastructure state:

{metrics_text}
{alerts_text}
{logs_text}
{context_section}

Provide analysis in JSON format:
{{
    "summary": "Brief assessment",
    "urgency": "critical|high|medium|low",
    "predictions": [
        {{
            "resource_name": "...",
            "failure_type": "...",
            "probability": 0.0-1.0,
            "indicators": ["..."]
        }}
    ],
    "root_cause": {{
        "primary_cause": "...", "confidence": 0.0-1.0, "contributing_factors": ["..."]
    }},
    "insights": ["..."],
    "recommended_actions": [
        {{"action": "...", "urgency": "...", "target": "...", "reason": "..."}}
    ]
}}"""

        response, tokens = await self._chat_completion(SYSTEM_PROMPT, user_prompt)
        total_tokens += tokens

        try:
            json_match = response.find("{")
            json_end = response.rfind("}") + 1
            if json_match != -1 and json_end > json_match:
                analysis_data = json.loads(response[json_match:json_end])
            else:
                analysis_data = {"summary": response, "urgency": "medium", "insights": []}
        except json.JSONDecodeError:
            analysis_data = {"summary": response, "urgency": "medium", "insights": []}

        result = AnalysisResult(
            id=str(uuid.uuid4()),
            summary=analysis_data.get("summary", "Analysis complete"),
            urgency=_safe_urgency(analysis_data.get("urgency", "medium")),
            insights=analysis_data.get("insights", []),
            metrics_analyzed=len(state.resources),
            logs_analyzed=len(state.recent_logs),
            alerts_analyzed=len(state.alerts),
            model_used=self.model,
            tokens_used=total_tokens,
            analysis_duration_seconds=time.time() - start_time,
        )

        for pred in analysis_data.get("predictions", []):
            result.predicted_failures.append(
                PredictedFailure(
                    resource_id="",
                    resource_name=pred.get("resource_name", "Unknown"),
                    failure_type=pred.get("failure_type", "Unknown"),
                    probability=pred.get("probability", 0.5),
                    confidence=pred.get("probability", 0.5),
                    indicators=pred.get("indicators", []),
                )
            )

        if analysis_data.get("root_cause"):
            rca = analysis_data["root_cause"]
            result.root_cause = RootCauseAnalysis(
                primary_cause=rca.get("primary_cause", "Unknown"),
                confidence=rca.get("confidence", 0.5),
                contributing_factors=rca.get("contributing_factors", []),
            )

        if result.requires_immediate_action() or result.has_predictions():
            result.remediation_plan = await self._generate_remediation_plan(
                analysis_data, result.urgency
            )

        logger.info(
            "Infrastructure analysis complete",
            urgency=result.urgency.value,
            duration=result.analysis_duration_seconds,
        )
        return result

    async def _generate_remediation_plan(
        self, analysis_data: dict[str, Any], urgency: Urgency
    ) -> RemediationPlan:
        steps = []
        for i, action in enumerate(analysis_data.get("recommended_actions", [])[:5], 1):
            action_name = action.get("action", "investigate")
            action_type = _safe_action_type(action_name)

            # All actions except NOTIFY and INVESTIGATE require human approval
            # This is enforced at execution time, but we mark it here for clarity
            step = RemediationStep(
                order=i,
                action_type=action_type,
                description=action.get("reason", action.get("action", "")),
                target_resource=action.get("target"),
                requires_approval=True,  # Always require approval - executor enforces this
            )
            steps.append(step)

        return RemediationPlan(
            id=str(uuid.uuid4()),
            title="AI-Generated Remediation Plan",
            description="Automated remediation based on infrastructure analysis",
            urgency=urgency,
            steps=steps,
            # SAFETY: Never auto-execute - all plans require human confirmation
            auto_executable=False,
        )
