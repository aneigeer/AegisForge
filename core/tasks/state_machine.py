"""任务状态机（V2.2 W03）：15 状态显式白名单，模型不能直接改状态。

PAUSE 只阻止后续步骤，不代表正在执行的 SQL 已停止；CANCEL 先进入
CANCEL_REQUESTED，在途操作需要核对后才能进入 CANCELLED 或 NEEDS_RECONCILIATION。
"""

from __future__ import annotations

from core.errors import StateInvalidError
from core.tasks.models import TERMINAL_STATUSES, TaskStatus

ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {
            TaskStatus.PLANNING,
            TaskStatus.CANCEL_REQUESTED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
        }
    ),
    TaskStatus.PLANNING: frozenset(
        {
            TaskStatus.OBSERVING,
            TaskStatus.DIAGNOSING,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.EXECUTING,
            TaskStatus.PAUSED,
            TaskStatus.CANCEL_REQUESTED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
        }
    ),
    TaskStatus.OBSERVING: frozenset(
        {
            TaskStatus.DIAGNOSING,
            TaskStatus.PLANNING,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.PAUSED,
            TaskStatus.CANCEL_REQUESTED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
        }
    ),
    TaskStatus.DIAGNOSING: frozenset(
        {
            TaskStatus.OBSERVING,
            TaskStatus.PLANNING,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.EXECUTING,
            TaskStatus.COMPLETED,
            TaskStatus.PAUSED,
            TaskStatus.CANCEL_REQUESTED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
        }
    ),
    TaskStatus.WAITING_APPROVAL: frozenset(
        {
            TaskStatus.PLANNING,
            TaskStatus.EXECUTING,
            TaskStatus.PAUSED,
            TaskStatus.CANCEL_REQUESTED,
            TaskStatus.MANUAL_INTERVENTION,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
        }
    ),
    TaskStatus.EXECUTING: frozenset(
        {
            TaskStatus.VERIFYING,
            TaskStatus.OBSERVING,
            TaskStatus.NEEDS_RECONCILIATION,
            TaskStatus.CANCEL_REQUESTED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
        }
    ),
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.NEEDS_RECONCILIATION,
            TaskStatus.MANUAL_INTERVENTION,
            TaskStatus.CANCEL_REQUESTED,
            TaskStatus.TIMED_OUT,
        }
    ),
    TaskStatus.PAUSED: frozenset(
        {
            TaskStatus.PLANNING,
            TaskStatus.OBSERVING,
            TaskStatus.DIAGNOSING,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.EXECUTING,
            TaskStatus.CANCEL_REQUESTED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
        }
    ),
    TaskStatus.CANCEL_REQUESTED: frozenset(
        {
            TaskStatus.CANCELLED,
            TaskStatus.NEEDS_RECONCILIATION,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.NEEDS_RECONCILIATION: frozenset(
        {
            TaskStatus.VERIFYING,
            TaskStatus.COMPLETED,
            TaskStatus.MANUAL_INTERVENTION,
            TaskStatus.CANCEL_REQUESTED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.MANUAL_INTERVENTION: frozenset(
        {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.TIMED_OUT: frozenset(
        {
            TaskStatus.NEEDS_RECONCILIATION,
            TaskStatus.MANUAL_INTERVENTION,
            TaskStatus.FAILED,
        }
    ),
}


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def ensure_transition(current: TaskStatus, target: TaskStatus) -> None:
    if current == target:
        return
    if not can_transition(current, target):
        raise StateInvalidError(
            "非法任务状态转换",
            details={"from": current.value, "to": target.value},
        )


def allowed_targets(current: TaskStatus) -> list[str]:
    return sorted(status.value for status in ALLOWED_TRANSITIONS.get(current, frozenset()))


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL_STATUSES
