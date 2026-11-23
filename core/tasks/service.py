"""Task Manager：创建、状态转换、暂停/取消、检查点与重启恢复。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from core.audit.events import AuditEventType, make_event
from core.errors import PolicyDeniedError, StateConflictError, StateInvalidError
from core.logging import get_logger
from core.approval.models import ApprovalGrant
from core.tasks.models import (
    Checkpoint,
    TERMINAL_STATUSES,
    Task,
    TaskEvent,
    TaskStatus,
    TaskStep,
    new_id,
    utc_now,
)
from core.tasks.repository import TaskRepository
from core.tasks.state_machine import allowed_targets, can_transition, ensure_transition
from core.tools.contracts import ToolContext, ToolResult, ToolStatus
from core.tools.executor import ToolExecutor
from core.tools.registry import ToolRegistry

logger = get_logger(__name__)


class TaskService:
    def __init__(
        self,
        repository: TaskRepository,
        registry: ToolRegistry,
        executor: ToolExecutor | None = None,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._executor = executor or ToolExecutor(registry, repository)

    @property
    def repository(self) -> TaskRepository:
        return self._repository

    @property
    def executor(self) -> ToolExecutor:
        return self._executor

    # ---- lifecycle -----------------------------------------------------
    def create_task(
        self,
        goal: str,
        *,
        tenant_id: str = "tenant_local",
        environment_id: str = "env_mysql_local",
        created_by: str = "user_local",
        risk_level: str = "R0",
        deadline_at: datetime | None = None,
        max_steps: int = 8,
        max_tool_calls: int = 10,
        max_tokens: int = 20_000,
        trace_id: str | None = None,
    ) -> Task:
        if not goal.strip():
            raise StateInvalidError("任务目标不能为空")
        now = utc_now()
        task = Task(
            task_id=new_id("task"),
            run_id=new_id("run"),
            goal=goal.strip(),
            status=TaskStatus.PENDING,
            risk_level=risk_level,  # type: ignore[arg-type]
            created_at=now,
            updated_at=now,
            tenant_id=tenant_id,
            environment_id=environment_id,
            created_by=created_by,
            state_version=1,
            deadline_at=deadline_at,
            max_steps=max_steps,
            max_tool_calls=max_tool_calls,
            max_tokens=max_tokens,
        )
        self._repository.insert_task(task)
        self._emit(
            AuditEventType.TASK_CREATED,
            task,
            {
                "goal": task.goal,
                "risk_level": task.risk_level,
                "tenant_id": task.tenant_id,
                "environment_id": task.environment_id,
                "state_version": task.state_version,
                "allowed_targets": allowed_targets(task.status),
            },
            trace_id=trace_id,
        )
        self.save_checkpoint(task.task_id, {"status": task.status.value, "reason": "created"})
        return self.get_task(task.task_id)

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> Task:
        return self._repository.get_task(task_id, tenant_id=tenant_id)

    def list_tasks(
        self,
        status: TaskStatus | None = None,
        limit: int = 50,
        *,
        tenant_id: str | None = None,
    ) -> list[Task]:
        return self._repository.list_tasks(status, limit, tenant_id=tenant_id)

    def transition(
        self,
        task_id: str,
        target: TaskStatus,
        *,
        expected_state_version: int | None = None,
        trace_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        ensure_transition(task.status, target)
        previous = task.status
        if previous == target:
            return task
        finished_at = utc_now() if target in TERMINAL_STATUSES else None
        try:
            task = self._repository.update_task_status(
                task_id,
                target,
                expected_state_version=(
                    task.state_version
                    if expected_state_version is None
                    else expected_state_version
                ),
                error_code=error_code,
                error_message=error_message,
                finished_at=finished_at,
                trace_id=trace_id,
            )
        except StateConflictError:
            raise
        self._emit(
            AuditEventType.TASK_STATE_CHANGED,
            task,
            {
                "from": previous.value,
                "to": target.value,
                "state_version": task.state_version,
                "error_code": error_code,
            },
            trace_id=trace_id,
        )
        self.save_checkpoint(
            task_id, {"status": target.value, "reason": "state_transition"}
        )
        return task

    # ---- pause / cancel ------------------------------------------------
    _PAUSABLE_STATUSES = frozenset(
        {
            TaskStatus.PLANNING,
            TaskStatus.OBSERVING,
            TaskStatus.DIAGNOSING,
            TaskStatus.WAITING_APPROVAL,
        }
    )
    _IN_FLIGHT_STATUSES = frozenset(
        {
            TaskStatus.EXECUTING,
            TaskStatus.VERIFYING,
            TaskStatus.NEEDS_RECONCILIATION,
        }
    )

    def request_pause(self, task_id: str, *, trace_id: str | None = None) -> Task:
        task = self.get_task(task_id)
        if task.status in TERMINAL_STATUSES:
            raise StateInvalidError(
                "任务已结束，无法暂停", details={"task_id": task_id, "status": task.status.value}
            )
        task = self._repository.set_task_flags(task_id, pause_requested=True)
        self._emit(
            AuditEventType.TASK_PAUSE_REQUESTED,
            task,
            {"status": task.status.value},
            trace_id=trace_id,
        )
        if task.status in self._PAUSABLE_STATUSES:
            task = self.transition(task_id, TaskStatus.PAUSED, trace_id=trace_id)
        # EXECUTING/VERIFYING 等状态只置标记：在途 SQL 是否停止必须通过核对确认。
        self.save_checkpoint(task_id, {"status": task.status.value, "reason": "pause_requested"})
        return task

    def resume(
        self,
        task_id: str,
        *,
        expected_state_version: int | None = None,
        trace_id: str | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        if task.status in TERMINAL_STATUSES:
            raise StateInvalidError(
                "任务已结束，无法恢复", details={"task_id": task_id, "status": task.status.value}
            )
        if expected_state_version is not None and task.state_version != expected_state_version:
            from core.errors import StateConflictError

            raise StateConflictError(
                "任务版本已变化，请重新读取后再恢复",
                details={
                    "task_id": task_id,
                    "expected_state_version": expected_state_version,
                    "actual_state_version": task.state_version,
                },
            )
        if not self._repository.environment_belongs_to(task.environment_id, task.tenant_id):
            raise StateInvalidError(
                "任务环境归属已变化，禁止直接恢复",
                details={
                    "task_id": task_id,
                    "environment_id": task.environment_id,
                    "tenant_id": task.tenant_id,
                },
            )
        if not task.pause_requested and task.status != TaskStatus.PAUSED:
            return task
        task = self._repository.set_task_flags(task_id, pause_requested=False)
        self._emit(
            AuditEventType.TASK_RESUMED,
            task,
            {"status": task.status.value},
            trace_id=trace_id,
        )
        if task.status == TaskStatus.PAUSED:
            task = self.transition(task_id, TaskStatus.PLANNING, trace_id=trace_id)
        self.save_checkpoint(task_id, {"status": task.status.value, "reason": "resumed"})
        return task

    def cancel(self, task_id: str, *, trace_id: str | None = None) -> Task:
        task = self.get_task(task_id)
        if task.status in TERMINAL_STATUSES:
            return task
        task = self._repository.set_task_flags(task_id, cancel_requested=True)
        self._emit(
            AuditEventType.TASK_CANCEL_REQUESTED,
            task,
            {"status": task.status.value},
            trace_id=trace_id,
        )
        in_flight = task.status in self._IN_FLIGHT_STATUSES
        # 所有非终态任务都先进入 CANCEL_REQUESTED，保证取消意图与结果分离。
        if can_transition(task.status, TaskStatus.CANCEL_REQUESTED):
            task = self.transition(
                task_id, TaskStatus.CANCEL_REQUESTED, trace_id=trace_id
            )
        if not in_flight and task.status == TaskStatus.CANCEL_REQUESTED:
            task = self.transition(task_id, TaskStatus.CANCELLED, trace_id=trace_id)
            self._emit(
                AuditEventType.TASK_CANCELLED,
                task,
                {"status": task.status.value},
                trace_id=trace_id,
            )
        self.save_checkpoint(task_id, {"status": task.status.value, "reason": "cancel_requested"})
        return task

    def mark_timeout(self, task_id: str, *, trace_id: str | None = None) -> Task:
        task = self.get_task(task_id)
        if task.status in TERMINAL_STATUSES or task.status == TaskStatus.TIMED_OUT:
            return task
        if not can_transition(task.status, TaskStatus.TIMED_OUT):
            return task
        return self.transition(task_id, TaskStatus.TIMED_OUT, trace_id=trace_id)

    # ---- checkpoints / recovery ----------------------------------------
    def save_checkpoint(self, task_id: str, state: dict[str, Any] | None = None) -> Checkpoint:
        task = self.get_task(task_id)
        payload = state or {
            "status": task.status.value,
            "steps": [
                {"step_id": step.step_id, "status": step.status}
                for step in self._repository.list_steps(task_id)
            ],
        }
        checkpoint = self._repository.save_checkpoint(task.task_id, task.run_id, payload)
        self._emit(
            AuditEventType.CHECKPOINT_SAVED,
            task,
            {"checkpoint_id": checkpoint.checkpoint_id, "sequence": checkpoint.sequence},
        )
        return checkpoint

    def latest_checkpoint(self, task_id: str) -> Checkpoint | None:
        return self._repository.latest_checkpoint(task_id)

    def recover_unfinished_tasks(
        self,
        *,
        trace_id: str | None = None,
        max_tasks: int | None = None,
        budget_s: float | None = None,
    ) -> list[Task]:
        """服务重启后恢复未完成任务。

        对处于 EXECUTING 的任务不做任何自动续跑，而是转入 NEEDS_RECONCILIATION，
        要求先核对在途写操作的真实状态；这符合“超时/中断不等于失败”的约束。

        `max_tasks` / `budget_s` 是可选的启动护栏：历史积压较多时，达到任一上限
        即停止继续处理，返回**已恢复**列表（类型与语义不变）。两者均为 None 时
        行为与旧实现一致（处理全部未完成任务）。护栏不新增审计事件、不改任何
        `transition` / `_emit` 语义，只收敛处理规模。
        """
        recovered: list[Task] = []
        started = time.monotonic()
        offset = 0
        batch_size = 100
        limit_reason: str | None = None
        while True:
            if max_tasks is not None and len(recovered) >= max_tasks:
                limit_reason = "max_tasks"
                break
            if budget_s is not None and time.monotonic() - started >= budget_s:
                limit_reason = "budget_s"
                break
            batch = self._repository.list_unfinished_tasks(
                limit=batch_size, offset=offset
            )
            if not batch:
                break
            for task in batch:
                checkpoint = self._repository.latest_checkpoint(task.task_id)
                requires_reconciliation = task.status == TaskStatus.EXECUTING
                if requires_reconciliation:
                    try:
                        task = self.transition(
                            task.task_id,
                            TaskStatus.NEEDS_RECONCILIATION,
                            trace_id=trace_id,
                        )
                    except StateConflictError:
                        # 其他进程已推进该任务，按最新状态继续记录恢复事件。
                        task = self.get_task(task.task_id)
                        requires_reconciliation = False
                self._emit(
                    AuditEventType.TASK_RECOVERED,
                    task,
                    {
                        "status": task.status.value,
                        "checkpoint_sequence": checkpoint.sequence if checkpoint else None,
                        "checkpoint_state": checkpoint.state if checkpoint else None,
                        "requires_reconciliation": requires_reconciliation,
                    },
                    trace_id=trace_id,
                )
                recovered.append(task)
                # 每恢复一条就检查上限，避免处理完一整批才发现已超限。
                if max_tasks is not None and len(recovered) >= max_tasks:
                    limit_reason = "max_tasks"
                    break
                if budget_s is not None and time.monotonic() - started >= budget_s:
                    limit_reason = "budget_s"
                    break
            if limit_reason is not None:
                break
            if len(batch) < batch_size:
                break
            offset += len(batch)
        if limit_reason is not None:
            logger.info(
                "启动恢复达到上限，提前停止",
                extra={"recovered": len(recovered), "limit_reason": limit_reason},
            )
        return recovered

    # ---- steps / tools -------------------------------------------------
    def add_step(
        self,
        task_id: str,
        *,
        title: str,
        tool_name: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> TaskStep:
        task = self.get_task(task_id)
        steps = self._repository.list_steps(task_id)
        now = utc_now()
        step = TaskStep(
            step_id=new_id("step"),
            task_id=task_id,
            run_id=task.run_id,
            step_index=len(steps),
            title=title,
            tool_name=tool_name,
            params=params or {},
            created_at=now,
            updated_at=now,
        )
        self._repository.insert_step(step)
        return step

    def run_tool(
        self,
        task_id: str,
        tool_name: str,
        params: dict[str, Any] | None,
        context: ToolContext,
        *,
        step_id: str | None = None,
        idempotency_key: str | None = None,
        approval_grant: ApprovalGrant | None = None,
    ) -> ToolResult:
        task = self.get_task(task_id)
        if task.pause_requested:
            raise PolicyDeniedError(
                "任务已暂停，不能发起新的工具调用",
                details={"task_id": task_id, "status": task.status.value},
            )
        result = self._executor.execute(
            tool_name,
            params,
            context,
            task=task,
            step_id=step_id,
            idempotency_key=idempotency_key,
            approval_grant=approval_grant,
        )
        self._converge_unknown_outcome(task_id, result)
        return result

    def _converge_unknown_outcome(self, task_id: str, result: ToolResult) -> None:
        """结果未知不得被解释成失败或成功：可迁移时立即转 NEEDS_RECONCILIATION。

        迁移不合法（例如任务已终态）时不做任何猜测，留给 Reconciler/人工核对；
        这里绝不重试工具（P0-18）。
        """
        if result.status is not ToolStatus.UNKNOWN_OUTCOME:
            return
        task = self.get_task(task_id)
        if task.status is TaskStatus.NEEDS_RECONCILIATION:
            return
        if not can_transition(task.status, TaskStatus.NEEDS_RECONCILIATION):
            return
        self.transition(
            task_id,
            TaskStatus.NEEDS_RECONCILIATION,
            error_code="UNKNOWN_OUTCOME",
            error_message="工具结果未知，禁止自动重试，需核对真实执行结果",
        )

    # ---- events --------------------------------------------------------
    def list_events(
        self, task_id: str, limit: int = 200, *, after_seq: int = 0
    ) -> list[TaskEvent]:
        return self._repository.list_events(task_id, limit, after_seq=after_seq)

    def _emit(
        self,
        event_type: AuditEventType,
        task: Task,
        payload: dict[str, Any],
        *,
        trace_id: str | None = None,
    ) -> None:
        self._repository.insert_event(
            make_event(
                event_type,
                task_id=task.task_id,
                run_id=task.run_id,
                payload=payload,
                trace_id=trace_id,
            ),
            trace_id=trace_id,
        )
