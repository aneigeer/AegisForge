"""W10 L1：`Reconciler.sweep_task()` 在**真实控制库**上的任务级过滤（P0-18 收窄）。

设计依据 `docs/w09/13-l1-l2-design.md` §3.5：

- 建 2 个真实任务（`TaskService`），各插 1 条 `StepPrepared`，`prepared_at`
  早于现在 5 分钟（保证 `older_than_s=0` 命中）；
- `sweep_task(tenant_id=..., task_id=A, older_than_s=0)` **只**返回 A 的 1 条结论，
  B 的任务状态与证据保持不动；
- 与「租户内历史残留共存」这一现实同跑：证明过滤发生在 **SQL 层**（同一份数据
  用租户级只读查询能看到 A 与 B 两行，用任务级核对只产出 1 条结论）。

纪律（`docs/w09/10-lead-rulings.md` §4 / §8）：

- 只 INSERT（`tasks` / `task_events` / `step_prepared`），**无** DELETE / UPDATE /
  TRUNCATE，**不**依赖 `DG_ADMIN_DSN`，**不**依赖 PG；
- 不断言历史残留条数、不清理残留（残留是合法证据，清理须走运维手册 + 人工审批）；
- 本文件不复用 `test_step_prepared.py` 的清理夹具（该夹具已改为只读核对）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from core.audit.service import AuditService
from core.execution.reconciler import (
    CONCLUSION_NEEDS_RECONCILIATION,
    REASON_NOT_OBSERVED,
    Reconciler,
)
from core.execution.step_records import StepPrepared, StepRecordRepository
from core.tasks.models import Task, TaskStatus, new_id, utc_now
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

TOOL_CALL_ID_PREFIX = "call"
RECONCILIATION_EVENT = "RECONCILIATION_REQUIRED"


@pytest.fixture()
def task_service(control_client) -> TaskService:
    return TaskService(TaskRepository(control_client), ToolRegistry())


@pytest.fixture()
def audit(control_client) -> AuditService:
    return AuditService(TaskRepository(control_client))


@pytest.fixture()
def step_records(control_client, audit) -> StepRecordRepository:
    return StepRecordRepository(control_client, audit=audit)


@pytest.fixture()
def reconciler(step_records, control_client, audit) -> Reconciler:
    return Reconciler(step_records, TaskRepository(control_client), audit)


def _executing_task(task_service: TaskService, goal: str) -> Task:
    """建一个停在 `EXECUTING` 的任务（可被核对推进状态）。"""
    task = task_service.create_task(goal)
    task_service.transition(task.task_id, TaskStatus.PLANNING)
    task = task_service.transition(task.task_id, TaskStatus.EXECUTING)
    assert task.status is TaskStatus.EXECUTING
    return task


def _prepared(task: Task) -> StepPrepared:
    """5 分钟前 prepared、无 observed 的证据行（`older_than_s=0` 必命中）。"""
    return StepPrepared(
        prepared_id=new_id("spr"),
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        run_id=task.run_id,
        tool_call_id=new_id(TOOL_CALL_ID_PREFIX),
        tool_name="mysql_update_rows",
        tool_version="1.0",
        policy_version="w06-v1",
        environment_id=task.environment_id,
        request={"table": "orders", "id": 1001, "status": "PAID"},
        approval_id=new_id("apr"),
        action_digest="a" * 64,
        idempotency_key=new_id("idem"),
        prepared_at=utc_now() - timedelta(minutes=5),
    )


def _reconciliation_events(audit: AuditService, task: Task) -> list:
    events, _ = audit.query(
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        event_types=[RECONCILIATION_EVENT],
        limit=10,
    )
    return list(events)


def test_sweep_task_only_converges_target_task(
    task_service, step_records, reconciler, audit, control_client
) -> None:
    """同一租户内两个任务的证据共存时，`sweep_task` 只动目标任务。"""
    task_a = _executing_task(task_service, "W10 L1 任务级核对 A")
    task_b = _executing_task(task_service, "W10 L1 任务级核对 B")
    prepared_a = _prepared(task_a)
    prepared_b = _prepared(task_b)
    step_records.insert_prepared(prepared_a)
    step_records.insert_prepared(prepared_b)

    # 前置事实：租户级**只读**查询能同时看到 A 与 B 的两行（含历史残留共存）。
    wide = step_records.find_unobserved(tenant_id=task_a.tenant_id, older_than_s=0, limit=500)
    wide_call_ids = {item.tool_call_id for item in wide}
    assert prepared_a.tool_call_id in wide_call_ids
    assert prepared_b.tool_call_id in wide_call_ids

    # 任务级核对：只针对 A。
    results = reconciler.sweep_task(
        tenant_id=task_a.tenant_id, task_id=task_a.task_id, older_than_s=0
    )

    assert len(results) == 1
    assert results[0].task_id == task_a.task_id
    assert results[0].tool_call_id == prepared_a.tool_call_id
    assert results[0].conclusion == CONCLUSION_NEEDS_RECONCILIATION
    assert results[0].reason == REASON_NOT_OBSERVED
    assert results[0].transitioned is True

    # A 已收敛；B 完全未被动过（状态 / 证据 / 审计三处都保持原样）。
    assert task_service.get_task(task_a.task_id).status is TaskStatus.NEEDS_RECONCILIATION
    assert task_service.get_task(task_b.task_id).status is TaskStatus.EXECUTING
    assert step_records.get_observed(prepared_b.tool_call_id) is None
    assert _reconciliation_events(audit, task_b) == []

    # B 的证据行仍在未观测集合里（没有被任何过滤逻辑误删/误判）。
    remaining = step_records.find_unobserved(
        tenant_id=task_b.tenant_id, older_than_s=0, task_id=task_b.task_id, limit=50
    )
    assert [item.tool_call_id for item in remaining] == [prepared_b.tool_call_id]

    # 只追加纪律：本用例没有产生工具调用记录（核对绝不重放）。
    rows = control_client.fetch_all(
        "SELECT COUNT(*) AS total FROM dg_system.tool_calls WHERE task_id IN (%s, %s)",
        (task_a.task_id, task_b.task_id),
    )
    assert int(rows[0]["total"]) == 0


def test_sweep_task_is_idempotent_for_already_converged_task(
    task_service, step_records, reconciler, audit
) -> None:
    """第二次任务级核对不重复迁移状态，只如实记录"已收敛"。"""
    task = _executing_task(task_service, "W10 L1 任务级核对幂等")
    prepared = _prepared(task)
    step_records.insert_prepared(prepared)

    first = reconciler.sweep_task(
        tenant_id=task.tenant_id, task_id=task.task_id, older_than_s=0
    )
    second = reconciler.sweep_task(
        tenant_id=task.tenant_id, task_id=task.task_id, older_than_s=0
    )

    assert len(first) == 1 and first[0].transitioned is True
    assert len(second) == 1
    assert second[0].task_id == task.task_id
    assert second[0].transitioned is False
    assert second[0].task_status == TaskStatus.NEEDS_RECONCILIATION.value
    assert task_service.get_task(task.task_id).status is TaskStatus.NEEDS_RECONCILIATION

    # 两条结论各写一条只追加事件，且都明确记录"未发生重放"。
    events = _reconciliation_events(audit, task)
    assert [event.payload["prepared_id"] for event in events] == [
        prepared.prepared_id,
        prepared.prepared_id,
    ]
    assert all(event.payload["replay_attempted"] is False for event in events)
