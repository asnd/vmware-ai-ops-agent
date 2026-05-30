"""
Tests for the action executor.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from vmware_ai_ops_agent.actions.executor import (
    ActionExecutor,
    ActionResult,
    ExecutionResult,
    ExecutionStatus,
)
from vmware_ai_ops_agent.analysis.models import (
    ActionType,
    RemediationPlan,
    RemediationStep,
    Urgency,
)
from vmware_ai_ops_agent.config import AgentConfig, AutoRemediateConfig


class TestActionExecutor:
    """Test suite for ActionExecutor."""

    @pytest.fixture
    def agent_config(self) -> AgentConfig:
        return AgentConfig(
            auto_remediate=AutoRemediateConfig(
                enabled=True,
                require_approval=False,
                max_actions_per_hour=10,
                allowed_actions=["vmotion", "drs_rebalance", "notify"],
                forbidden_actions=["host_maintenance"],
            )
        )

    @pytest.fixture
    def mock_vcenter(self):
        vcenter = AsyncMock()
        vcenter.config = MagicMock()
        vcenter.config.dry_run = True
        vcenter.vmotion_vm = AsyncMock(return_value={"dry_run": True, "action": "vmotion"})
        vcenter.find_best_target_host = AsyncMock(return_value="host-01")
        return vcenter

    @pytest.fixture
    def executor(self, agent_config: AgentConfig, mock_vcenter) -> ActionExecutor:
        return ActionExecutor(agent_config, vcenter=mock_vcenter)

    @pytest.fixture
    def sample_plan(self) -> RemediationPlan:
        return RemediationPlan(
            id="plan-001",
            title="Test Remediation",
            description="Test plan for unit tests",
            urgency=Urgency.LOW,
            auto_executable=True,
            steps=[
                RemediationStep(
                    order=1,
                    action_type=ActionType.VMOTION,
                    description="Move VM to less loaded host",
                    target_resource="vm-001",
                    parameters={"target_host": "host-02"},
                    requires_approval=False,
                    estimated_duration="2 minutes",
                ),
                RemediationStep(
                    order=2,
                    action_type=ActionType.NOTIFY,
                    description="Notify ops team",
                    requires_approval=False,
                    estimated_duration="1 second",
                ),
            ],
        )

    @pytest.mark.asyncio
    async def test_execute_plan_success(
        self, executor: ActionExecutor, sample_plan: RemediationPlan
    ):
        """Plan execution should complete successfully."""
        result = await executor.execute_plan(sample_plan, dry_run=True)

        assert result.status == ExecutionStatus.COMPLETED
        assert len(result.action_results) == 2
        assert all(r.success for r in result.action_results)

    @pytest.mark.asyncio
    async def test_forbidden_action_skipped(self, executor: ActionExecutor):
        """Forbidden actions should be skipped."""
        plan = RemediationPlan(
            id="plan-002",
            title="Forbidden Action Plan",
            description="Plan with forbidden action",
            urgency=Urgency.HIGH,
            steps=[
                RemediationStep(
                    order=1,
                    action_type=ActionType.HOST_MAINTENANCE,
                    description="Enter Maintenance Mode",
                    target_resource="vm-001",
                    requires_approval=False,
                    estimated_duration="30 seconds",
                ),
            ],
        )

        result = await executor.execute_plan(plan, dry_run=True)

        assert len(result.action_results) == 1
        assert result.action_results[0].status == ExecutionStatus.SKIPPED
        assert "Not allowed" in (result.action_results[0].error or "")

    @pytest.mark.asyncio
    async def test_rate_limiting(self, agent_config: AgentConfig, mock_vcenter):
        """Actions should be rate limited."""
        agent_config.auto_remediate.max_actions_per_hour = 2
        executor = ActionExecutor(agent_config, vcenter=mock_vcenter)

        plan = RemediationPlan(
            id="plan-003",
            title="Many Actions Plan",
            description="Plan with many actions",
            urgency=Urgency.LOW,
            steps=[
                RemediationStep(
                    order=i,
                    action_type=ActionType.NOTIFY,
                    description=f"Notify {i}",
                    requires_approval=False,
                    estimated_duration="1 second",
                )
                for i in range(5)
            ],
        )

        result = await executor.execute_plan(plan, dry_run=True)

        completed = [r for r in result.action_results if r.status == ExecutionStatus.COMPLETED]
        skipped = [r for r in result.action_results if r.status == ExecutionStatus.SKIPPED]

        assert len(completed) <= 2
        assert len(skipped) >= 3

    @pytest.mark.asyncio
    async def test_approval_required_skipped(self, agent_config: AgentConfig, mock_vcenter):
        """Actions requiring approval should be skipped without callback."""
        agent_config.auto_remediate.require_approval = True
        executor = ActionExecutor(agent_config, vcenter=mock_vcenter)

        plan = RemediationPlan(
            id="plan-004",
            title="Approval Required Plan",
            description="Plan requiring approval",
            urgency=Urgency.MEDIUM,
            steps=[
                RemediationStep(
                    order=1,
                    action_type=ActionType.VMOTION,
                    description="Move VM",
                    target_resource="vm-001",
                    requires_approval=True,
                    estimated_duration="2 minutes",
                ),
            ],
        )

        result = await executor.execute_plan(plan, dry_run=True)

        assert result.action_results[0].status == ExecutionStatus.SKIPPED
        assert "Approval" in (result.action_results[0].error or "")

    @pytest.mark.asyncio
    async def test_approval_callback_allows_action(self, agent_config: AgentConfig, mock_vcenter):
        """Actions should proceed when approval callback returns True."""
        agent_config.auto_remediate.require_approval = True
        executor = ActionExecutor(agent_config, vcenter=mock_vcenter)

        plan = RemediationPlan(
            id="plan-005",
            title="Approved Plan",
            description="Plan with approval",
            urgency=Urgency.MEDIUM,
            steps=[
                RemediationStep(
                    order=1,
                    action_type=ActionType.VMOTION,
                    description="Move VM",
                    target_resource="vm-001",
                    parameters={"target_host": "host-02"},
                    requires_approval=True,
                    estimated_duration="2 minutes",
                ),
            ],
        )

        result = await executor.execute_plan(
            plan, dry_run=True, approval_callback=lambda step: True
        )

        assert result.action_results[0].status == ExecutionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_safe_action_not_blocked_by_global_approval(self, agent_config: AgentConfig):
        """Safe actions should not need callback when only global approval is enabled."""
        agent_config.auto_remediate.require_approval = True
        executor = ActionExecutor(agent_config, vcenter=None)

        plan = RemediationPlan(
            id="plan-006",
            title="Safe Action Plan",
            description="Safe action with global approval enabled",
            urgency=Urgency.LOW,
            steps=[
                RemediationStep(
                    order=1,
                    action_type=ActionType.NOTIFY,
                    description="Notify ops team",
                    requires_approval=False,
                    estimated_duration="1 second",
                ),
            ],
        )

        result = await executor.execute_plan(plan, dry_run=True)

        assert result.action_results[0].status == ExecutionStatus.COMPLETED


class TestExecutorCrossCycleState:
    """Verify that rate-limit and dedup state survive across execute_plan calls (A1 fix)."""

    @pytest.fixture
    def shared_executor(self) -> ActionExecutor:
        config = AgentConfig(
            auto_remediate=AutoRemediateConfig(
                enabled=True,
                require_approval=False,
                max_actions_per_hour=5,
                allowed_actions=["notify", "investigate"],
                forbidden_actions=[],
            )
        )
        return ActionExecutor(config)

    def _notify_plan(self, plan_id: str, target: str = "vm-001") -> RemediationPlan:
        return RemediationPlan(
            id=plan_id,
            title="Notify Plan",
            description="",
            urgency=Urgency.LOW,
            auto_executable=True,
            steps=[
                RemediationStep(
                    order=1,
                    action_type=ActionType.NOTIFY,
                    description="Notify",
                    target_resource=target,
                    requires_approval=False,
                    estimated_duration="1s",
                )
            ],
        )

    @pytest.mark.asyncio
    async def test_rate_limit_accumulates_across_plans(self, shared_executor: ActionExecutor):
        """Actions counted across multiple execute_plan calls should hit the hourly cap."""
        shared_executor.config.auto_remediate.max_actions_per_hour = 3

        completed_total = 0
        for i in range(5):
            result = await shared_executor.execute_plan(self._notify_plan(f"plan-{i}"))
            completed_total += sum(1 for r in result.action_results if r.success)

        assert completed_total <= 3, "Hourly rate limit must persist across execute_plan calls"

    @pytest.mark.asyncio
    async def test_dedup_skips_same_action_across_plans(self, shared_executor: ActionExecutor):
        """The same (action_type, target_resource) within the dedup window must be skipped
        even when submitted via a separate execute_plan call (i.e., a separate cycle)."""
        # First call — action should succeed
        result1 = await shared_executor.execute_plan(self._notify_plan("plan-A", target="vm-dedup"))
        assert result1.action_results[0].success is True

        # Immediately second call with identical action — should be deduplicated
        result2 = await shared_executor.execute_plan(self._notify_plan("plan-B", target="vm-dedup"))
        assert result2.action_results[0].status == ExecutionStatus.SKIPPED
        assert "Duplicate" in (result2.action_results[0].error or "")

    @pytest.mark.asyncio
    async def test_different_targets_not_deduped(self, shared_executor: ActionExecutor):
        """Actions on different targets must not be conflated by the dedup check."""
        result1 = await shared_executor.execute_plan(self._notify_plan("plan-X", target="vm-001"))
        result2 = await shared_executor.execute_plan(self._notify_plan("plan-Y", target="vm-002"))

        assert result1.action_results[0].success is True
        assert result2.action_results[0].success is True


class TestActionResult:
    """Test suite for ActionResult."""

    def test_success_property(self):
        """Success property should reflect completion status."""
        step = RemediationStep(
            order=1,
            action_type=ActionType.NOTIFY,
            description="Test",
            requires_approval=False,
            estimated_duration="1 second",
        )

        completed = ActionResult(step=step, status=ExecutionStatus.COMPLETED)
        failed = ActionResult(step=step, status=ExecutionStatus.FAILED)
        skipped = ActionResult(step=step, status=ExecutionStatus.SKIPPED)

        assert completed.success is True
        assert failed.success is False
        assert skipped.success is False


class TestExecutionResult:
    """Test suite for ExecutionResult."""

    def test_failed_actions_property(self):
        """Failed actions should be correctly identified."""
        plan = RemediationPlan(
            id="plan-006",
            title="Test Plan",
            description="Test",
            urgency=Urgency.LOW,
            steps=[],
        )
        step = RemediationStep(
            order=1,
            action_type=ActionType.NOTIFY,
            description="Test",
            requires_approval=False,
            estimated_duration="1 second",
        )

        result = ExecutionResult(
            plan=plan,
            status=ExecutionStatus.COMPLETED,
            action_results=[
                ActionResult(step=step, status=ExecutionStatus.COMPLETED),
                ActionResult(step=step, status=ExecutionStatus.FAILED, error="Test error"),
                ActionResult(step=step, status=ExecutionStatus.COMPLETED),
            ],
        )

        assert len(result.failed_actions) == 1
        assert result.failed_actions[0].error == "Test error"
