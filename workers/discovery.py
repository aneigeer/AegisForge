"""候选发现与**参数还原**（只读，全部经既有仓储/服务接口，禁止裸 SQL）。

候选定义（三条同时成立）：

1. 任务状态为 `WAITING_APPROVAL`，且未请求暂停/取消；
2. 任务没有仍然有效的活动租约（有则说明别的 Worker 正在处理，本轮不抢）；
3. 存在 `status=APPROVED` 且未过期的审批——只有"人工已批准"才可能进入执行链。

**为什么只取 `WAITING_APPROVAL`（而不是同时接受 `EXECUTING`）**：

- `ApprovalService.approve()` 完全不改任务状态，所有 `transition` 都作用在 approvals
  行上，因此审批通过后任务**仍停在 `WAITING_APPROVAL`**；
- `Worker` 必须在建信封**之前**把任务迁到 `EXECUTING`，否则一旦出现
  `UNKNOWN_OUTCOME`，`ToolGateway._converge_unknown_outcome()` 的
  `can_transition(WAITING_APPROVAL, NEEDS_RECONCILIATION)` 为假会静默 `return`，
  任务将卡在 `WAITING_APPROVAL` 且永远不会被标记"待核对"；
- 迁移顺序是硬约束：transition → 重新读取 `state_version` → 建信封 → 执行
  （Gateway 第 5 步强校验 `state_version`，用旧值必抛 `StateConflictError`）。

参数还原（W07 收口最关键的 fail-closed 点）：

- `approvals` 表**没有** `parameters_json`，只有 `parameters_digest`；原始参数只能
  来自 `task_steps.params_json`；
- 命中规则：`approval.step_id` 精确命中步骤 → 取该步骤参数；`step_id` 为 NULL 时
  回退"同 `tool_name` 的最后一个未完成步骤"；
- **两条都拿不到就绝不执行**：写 `RECONCILIATION_REQUIRED` 审计事件 + 结构化告警，
  交人工核对。宁可漏执行，也不能猜参数——猜错参数等于绕过审批对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from core.approval.models import ApprovalDetail, ApprovalStatus
from core.audit.events import AuditEventType, make_event
from core.logging import get_logger
from core.tasks.models import Task, TaskStatus, TaskStep, utc_now

logger = get_logger(__name__)

#: 唯一允许进入执行链的任务状态。
#:
#: 只取 `WAITING_APPROVAL`：审批通过不改任务状态，所以"已批准待执行"的真实形态
#: 就是这个状态；`EXECUTING` 说明已经有 Worker 迁移过它（可能在执行中，属于
#: "已消费/在途"，绝不能再被第二个循环当成候选）。
CANDIDATE_TASK_STATUSES: tuple[TaskStatus, ...] = (TaskStatus.WAITING_APPROVAL,)
#: 视为"已完成"的步骤状态；其余（PENDING/RUNNING/未知）都算未完成。
FINISHED_STEP_STATUSES: frozenset[str] = frozenset({"COMPLETED", "SKIPPED", "FAILED"})
#: 单任务审批扫描上限（同一任务通常只有 1 条获批审批）。
_APPROVAL_SCAN_LIMIT = 20
#: 参数还原失败的结构化告警标识（日志侧用于告警规则匹配）。
ALERT_PARAMS_UNRESOLVED = "WORKER_PARAMS_UNRESOLVED"


class TaskReaderPort(Protocol):
    """`core.tasks.repository.TaskRepository` 的只读子集。"""

    def list_tasks(
        self,
        status: TaskStatus | None = None,
        limit: int = 50,
        *,
        tenant_id: str | None = None,
    ) -> list[Task]: ...

    def list_steps(self, task_id: str) -> list[TaskStep]: ...

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> Task: ...


class ApprovalReaderPort(Protocol):
    """`core.approval.service.ApprovalService` 的只读子集。"""

    def list(
        self,
        *,
        tenant_id: str,
        status: ApprovalStatus | None = None,
        task_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[ApprovalDetail], str | None]: ...


class LeaseLookupPort(Protocol):
    def get_active(self, task_id: str) -> Any: ...


class LeaseServicePort(Protocol):
    repository: LeaseLookupPort


class AuditPort(Protocol):
    def try_emit(self, event: Any, *, actor_user_id: str | None = None) -> bool: ...


class DiscoveryStackPort(Protocol):
    """`workers.stack.WorkerStack` 中候选发现所需的最小面。"""

    tenant_id: str
    repository: TaskReaderPort
    approvals: ApprovalReaderPort
    lease_service: LeaseServicePort
    audit: AuditPort


@dataclass(frozen=True)
class ResolvedParams:
    """从控制库事实还原出的执行参数。"""

    tool_name: str
    params: dict[str, Any]
    step_id: str | None


@dataclass(frozen=True)
class Candidate:
    """一条可执行候选：任务 + 已批准审批 + 还原后的参数。"""

    task: Task
    approval: ApprovalDetail
    tool_name: str
    params: dict[str, Any] = field(default_factory=dict)
    step_id: str | None = None

    @property
    def task_id(self) -> str:
        return self.task.task_id

    @property
    def approval_id(self) -> str:
        return self.approval.approval_id

    @property
    def requested_by(self) -> str:
        """审批申请人；作为首选执行主体（见 W07 收口决议 §7）。"""
        return str(self.approval.requested_by or "")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _has_live_lease(stack: DiscoveryStackPort, task_id: str) -> bool:
    """任务是否被别的 Worker 持有（未过期的 ACTIVE 租约）。"""
    lease = stack.lease_service.repository.get_active(task_id)
    if lease is None:
        return False
    expires_at = getattr(lease, "expires_at", None)
    if not isinstance(expires_at, datetime):
        # 无法判定过期时间时按"仍被持有"处理：抢租约不是本模块的职责。
        return True
    return _as_utc(expires_at) > utc_now()


def _reject_params(
    stack: DiscoveryStackPort,
    *,
    task: Task,
    approval: ApprovalDetail,
    reason: str,
) -> None:
    """参数无法还原：写核对事件 + 告警，返回 None（绝不猜参数）。"""
    logger.error(
        "无法从控制库还原审批参数，拒绝自动执行",
        extra={
            "alert": ALERT_PARAMS_UNRESOLVED,
            "reason": reason,
            "task_id": task.task_id,
            "approval_id": approval.approval_id,
            "step_id": approval.step_id,
            "tool_name": approval.tool_name,
        },
    )
    stack.audit.try_emit(
        make_event(
            AuditEventType.RECONCILIATION_REQUIRED,
            task_id=task.task_id,
            run_id=task.run_id,
            payload={
                "reason": reason,
                "source": "worker_discovery",
                "approval_id": approval.approval_id,
                "step_id": approval.step_id,
                "tool_name": approval.tool_name,
                "action": "DO_NOT_EXECUTE",
            },
        )
    )


def resolve_execution_params(
    stack: DiscoveryStackPort,
    *,
    task: Task,
    approval: ApprovalDetail,
) -> ResolvedParams | None:
    """按 `approval.step_id` → 未完成步骤回退的顺序还原参数；失败返回 None。"""
    steps = stack.repository.list_steps(task.task_id)

    step: TaskStep | None = None
    if approval.step_id:
        step = next(
            (item for item in steps if item.step_id == approval.step_id), None
        )
        if step is None:
            _reject_params(
                stack, task=task, approval=approval, reason="STEP_NOT_FOUND"
            )
            return None
        if step.tool_name is not None and step.tool_name != approval.tool_name:
            # 步骤与审批指向不同工具：参数与审批对象不同源，直接停。
            _reject_params(
                stack, task=task, approval=approval, reason="STEP_TOOL_MISMATCH"
            )
            return None
    else:
        unfinished = [
            item
            for item in steps
            if item.tool_name == approval.tool_name
            and str(item.status) not in FINISHED_STEP_STATUSES
        ]
        unfinished.sort(key=lambda item: (item.step_index, item.step_id))
        step = unfinished[-1] if unfinished else None
        if step is None:
            _reject_params(
                stack, task=task, approval=approval, reason="NO_UNFINISHED_STEP"
            )
            return None

    return ResolvedParams(
        tool_name=approval.tool_name,
        params=dict(step.params or {}),
        step_id=step.step_id,
    )


def _approved_approval(
    stack: DiscoveryStackPort, task: Task
) -> ApprovalDetail | None:
    """挑一条"已批准且未过期"的审批；多条时取最早创建（确定性）。"""
    approvals, _ = stack.approvals.list(
        tenant_id=stack.tenant_id,
        status=ApprovalStatus.APPROVED,
        task_id=task.task_id,
        limit=_APPROVAL_SCAN_LIMIT,
    )
    usable = [detail for detail in approvals if not detail.is_expired()]
    if not usable:
        return None
    usable.sort(key=lambda detail: (detail.created_at, detail.approval_id))
    return usable[0]


def discover_candidates(
    stack: DiscoveryStackPort, *, limit: int = 10
) -> list[Candidate]:
    """发现可执行候选（按任务创建时间升序，任务维度去重，最多 `limit` 条）。"""
    if limit < 1:
        return []

    # 每个状态多取一些，避免"某个状态的候选全被租约占住"时漏掉另一状态的候选。
    scan_limit = max(limit * 2, limit)
    tasks: dict[str, Task] = {}
    for status in CANDIDATE_TASK_STATUSES:
        for task in stack.repository.list_tasks(
            status, limit=scan_limit, tenant_id=stack.tenant_id
        ):
            tasks.setdefault(task.task_id, task)

    ordered = sorted(tasks.values(), key=lambda item: (item.created_at, item.task_id))

    candidates: list[Candidate] = []
    for task in ordered:
        if len(candidates) >= limit:
            break
        if task.cancel_requested or task.pause_requested:
            continue
        if task.status not in CANDIDATE_TASK_STATUSES:
            continue
        if _has_live_lease(stack, task.task_id):
            continue
        approval = _approved_approval(stack, task)
        if approval is None:
            continue
        resolved = resolve_execution_params(stack, task=task, approval=approval)
        if resolved is None:
            continue
        candidates.append(
            Candidate(
                task=task,
                approval=approval,
                tool_name=resolved.tool_name,
                params=resolved.params,
                step_id=resolved.step_id,
            )
        )
    return candidates


__all__ = [
    "ALERT_PARAMS_UNRESOLVED",
    "CANDIDATE_TASK_STATUSES",
    "FINISHED_STEP_STATUSES",
    "Candidate",
    "DiscoveryStackPort",
    "ResolvedParams",
    "discover_candidates",
    "resolve_execution_params",
]
