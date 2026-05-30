"""
Action executor for remediation plans.

Supports AriaOps MCP write operations for maintenance mode and
alert management when ariaops_client is provided.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import structlog

from ..analysis.models import ActionType, RemediationPlan, RemediationStep
from ..config import AgentConfig
from .notifications import NotificationService
from .vcenter import VCenterClient

logger = structlog.get_logger(__name__)


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class RecentAction:
    action_type: ActionType
    target_resource: str | None
    executed_at: datetime


@dataclass
class ActionResult:
    step: RemediationStep
    status: ExecutionStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.status == ExecutionStatus.COMPLETED


@dataclass
class ExecutionResult:
    plan: RemediationPlan
    status: ExecutionStatus
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None
    action_results: list[ActionResult] = field(default_factory=list)
    dry_run: bool = False

    @property
    def success(self) -> bool:
        return self.status == ExecutionStatus.COMPLETED

    @property
    def failed_actions(self) -> list[ActionResult]:
        return [r for r in self.action_results if r.status == ExecutionStatus.FAILED]


class ActionExecutor:
    """Executor for remediation actions.

    Approval can be enforced globally (for non-safe actions) via
    auto_remediate.require_approval, or explicitly per-step using
    RemediationStep.requires_approval.

    Supports deduplication: actions with the same (action_type, target_resource)
    within DEDUP_WINDOW_MINUTES are skipped to prevent repeated remediation
    across consecutive analysis cycles.
    """

    # Actions that are safe to execute without human confirmation
    # Only logging/notification actions are considered safe
    SAFE_ACTIONS = frozenset({ActionType.NOTIFY, ActionType.INVESTIGATE})

    # Time window for deduplicating identical actions
    DEDUP_WINDOW_MINUTES: int = 30

    def __init__(
        self,
        agent_config: AgentConfig,
        vcenter: VCenterClient | None = None,
        notifications: NotificationService | None = None,
        ariaops_client: Any = None,
    ):
        self.config = agent_config
        self.vcenter = vcenter
        self.notifications = notifications
        self.ariaops_client = ariaops_client
        self._action_count_hour = 0
        self._hour_start = datetime.utcnow()
        self._recent_actions: deque[RecentAction] = deque(maxlen=200)

        self._handlers: dict[ActionType, Callable] = {
            ActionType.VMOTION: self._execute_vmotion,
            ActionType.STORAGE_VMOTION: self._execute_storage_vmotion,
            ActionType.DRS_REBALANCE: self._execute_drs_rebalance,
            ActionType.HOST_MAINTENANCE: self._execute_host_maintenance,
            ActionType.NOTIFY: self._execute_notify,
            ActionType.INVESTIGATE: self._execute_investigate,
        }

    def _check_rate_limit(self) -> bool:
        now = datetime.utcnow()
        if (now - self._hour_start).total_seconds() >= 3600:
            self._action_count_hour = 0
            self._hour_start = now

        if self._action_count_hour >= self.config.auto_remediate.max_actions_per_hour:
            logger.warning("Action rate limit reached")
            return False
        return True

    def _is_action_allowed(self, action_type: ActionType) -> bool:
        if action_type.value in self.config.auto_remediate.forbidden_actions:
            return False
        if self.config.auto_remediate.allowed_actions:
            return action_type.value in self.config.auto_remediate.allowed_actions
        return True

    def _is_duplicate(self, step: RemediationStep) -> bool:
        """Check if this exact action was already executed within the dedup window."""
        cutoff = datetime.utcnow() - timedelta(minutes=self.DEDUP_WINDOW_MINUTES)
        for recent in self._recent_actions:
            if (
                recent.action_type == step.action_type
                and recent.target_resource == step.target_resource
                and recent.executed_at > cutoff
            ):
                return True
        return False

    def _requires_human_approval(self, step: RemediationStep) -> bool:
        if step.requires_approval:
            return True
        return (
            self.config.auto_remediate.require_approval
            and step.action_type not in self.SAFE_ACTIONS
        )

    async def execute_plan(
        self,
        plan: RemediationPlan,
        dry_run: bool | None = None,
        approval_callback: Callable[[RemediationStep], bool] | None = None,
    ) -> ExecutionResult:
        if dry_run is None and self.vcenter:
            dry_run = self.vcenter.config.dry_run

        result = ExecutionResult(
            plan=plan, status=ExecutionStatus.RUNNING, dry_run=dry_run or False
        )

        logger.info(
            "Starting plan execution",
            plan_id=plan.id,
            steps=len(plan.steps),
            dry_run=dry_run,
        )

        try:
            for step in plan.steps:
                if not self._check_rate_limit():
                    result.action_results.append(
                        ActionResult(step=step, status=ExecutionStatus.SKIPPED, error="Rate limit")
                    )
                    continue

                if not self._is_action_allowed(step.action_type):
                    result.action_results.append(
                        ActionResult(step=step, status=ExecutionStatus.SKIPPED, error="Not allowed")
                    )
                    continue

                if self._is_duplicate(step):
                    logger.info(
                        "Skipping duplicate action within dedup window",
                        action=step.action_type.value,
                        target=step.target_resource,
                        window_minutes=self.DEDUP_WINDOW_MINUTES,
                    )
                    result.action_results.append(
                        ActionResult(
                            step=step,
                            status=ExecutionStatus.SKIPPED,
                            error=f"Duplicate action within {self.DEDUP_WINDOW_MINUTES}min window",
                        )
                    )
                    continue

                requires_human_approval = self._requires_human_approval(step)

                if requires_human_approval:
                    if not approval_callback:
                        logger.warning(
                            "Action requires human confirmation but no callback provided",
                            action=step.action_type.value,
                            target=step.target_resource,
                        )
                        result.action_results.append(
                            ActionResult(
                                step=step,
                                status=ExecutionStatus.SKIPPED,
                                error="Approval required - no approval callback",
                            )
                        )
                        continue

                    # Always require explicit approval for non-safe actions
                    approved = approval_callback(step)
                    if not approved:
                        logger.info(
                            "Action rejected by human operator",
                            action=step.action_type.value,
                            target=step.target_resource,
                        )
                        result.action_results.append(
                            ActionResult(
                                step=step,
                                status=ExecutionStatus.SKIPPED,
                                error="Rejected by human operator",
                            )
                        )
                        continue

                    logger.info(
                        "Action approved by human operator",
                        action=step.action_type.value,
                        target=step.target_resource,
                    )

                action_result = await self._execute_step(step, dry_run or False)
                result.action_results.append(action_result)

                if action_result.success:
                    self._action_count_hour += 1
                    self._recent_actions.append(
                        RecentAction(
                            action_type=step.action_type,
                            target_resource=step.target_resource,
                            executed_at=datetime.utcnow(),
                        )
                    )

                # Non-safe actions failing should stop the plan
                if not action_result.success and requires_human_approval:
                    result.status = ExecutionStatus.FAILED
                    break
            else:
                result.status = ExecutionStatus.COMPLETED

        except Exception as e:
            logger.error("Plan execution failed", error=str(e))
            result.status = ExecutionStatus.FAILED

        result.completed_at = datetime.utcnow()
        logger.info("Plan execution complete", status=result.status.value)
        return result

    async def _execute_step(self, step: RemediationStep, dry_run: bool) -> ActionResult:
        result = ActionResult(
            step=step, status=ExecutionStatus.RUNNING, started_at=datetime.utcnow()
        )

        try:
            handler = self._handlers.get(step.action_type, self._execute_investigate)
            output = await handler(step, dry_run)
            result.output = output
            result.status = ExecutionStatus.COMPLETED
        except Exception as e:
            logger.error("Step execution failed", error=str(e))
            result.status = ExecutionStatus.FAILED
            result.error = str(e)

        result.completed_at = datetime.utcnow()
        return result

    async def _execute_vmotion(self, step: RemediationStep, dry_run: bool) -> dict[str, Any]:
        if not self.vcenter:
            raise RuntimeError("vCenter client not configured")

        vm_id = step.target_resource
        target_host = step.parameters.get("target_host")
        if not target_host:
            target_host = await self.vcenter.find_best_target_host(vm_id or "")

        if not vm_id or not target_host:
            raise ValueError("Missing VM or target host")

        return await self.vcenter.vmotion_vm(vm_id, target_host)

    async def _execute_storage_vmotion(
        self, step: RemediationStep, dry_run: bool
    ) -> dict[str, Any]:
        if not self.vcenter:
            raise RuntimeError("vCenter client not configured")

        vm_id = step.target_resource
        target_ds = step.parameters.get("target_datastore")
        if not target_ds:
            target_ds = await self.vcenter.find_best_target_datastore(vm_id or "")

        if not vm_id or not target_ds:
            raise ValueError("Missing VM or target datastore")

        return await self.vcenter.storage_vmotion_vm(vm_id, target_ds)

    async def _execute_drs_rebalance(self, step: RemediationStep, dry_run: bool) -> dict[str, Any]:
        if not self.vcenter:
            raise RuntimeError("vCenter client not configured")

        cluster_id = step.target_resource or "default"
        return await self.vcenter.trigger_drs_recommendation(cluster_id)

    async def _execute_notify(self, step: RemediationStep, dry_run: bool) -> dict[str, Any]:
        logger.info("Notification action", message=step.description)
        return {"action": "notify", "message": step.description}

    async def _execute_investigate(self, step: RemediationStep, dry_run: bool) -> dict[str, Any]:
        logger.info("Investigation required", description=step.description)
        return {"action": "investigate", "description": step.description}

    async def _execute_host_maintenance(
        self, step: RemediationStep, dry_run: bool
    ) -> dict[str, Any]:
        """Put a host into maintenance mode via AriaOps MCP."""
        resource_id = step.target_resource
        if not resource_id:
            raise ValueError("Missing target resource for maintenance mode")

        duration = step.parameters.get("duration_minutes", 60)

        if dry_run:
            logger.info(
                "DRY RUN: Would mark resource as maintained",
                resource_id=resource_id,
                duration=duration,
            )
            return {"dry_run": True, "action": "host_maintenance", "resource_id": resource_id}

        if not self.ariaops_client:
            raise RuntimeError("AriaOps MCP client not configured for maintenance operations")

        result = await self.ariaops_client.mark_resources_maintained(
            resource_ids=[resource_id],
            duration_minutes=duration,
        )
        logger.info("Resource marked as maintained", resource_id=resource_id, result=result)
        return {"action": "host_maintenance", "resource_id": resource_id, "result": result}
