"""W08 T05 / AC-16：`UNKNOWN` 收敛不重放（真实控制库 + 真实工具调用计数）。

两例都做同一组硬断言：

1. 收敛态正确——证据充分 → `NEEDS_RECONCILIATION`，证据不足 → `MANUAL_INTERVENTION`；
2. 恢复**前后** `SELECT COUNT(*) FROM dg_system.tool_calls` **相等**（零增长），
   这是「恢复器没有重放写」在库层面的可断言证据；
3. 任务链上只出现 `AGENT_RECOVERY_STOPPED`，`payload.tool_calls_delta == 0`，且没有任何
   `TOOL_CALL_*` 事件（恢复器不产生工具调用）。

装配口径与 T03 一致：`tasks` / `audit` 用真实控制库服务，恢复器本身不持有
registry / gateway / DSN，因此本用例不需要（也不应该）给它任何执行面。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from core.audit.service import AuditService
from core.recovery.controller import RecoveryController
from core.tasks.models import TaskStatus
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

TENANT_ID = "tenant_local"
TOOL_CALLS_COUNT_SQL = "SELECT COUNT(*) AS total FROM dg_system.tool_calls"


# ---------------------------------------------------------------------------
# 真实控制库装配
# ---------------------------------------------------------------------------


@pytest.fixture
def recovery_stack(settings, control_client):
    """真实 TaskService / AuditService + 处于 `EXECUTING` 的任务。"""
    repository = TaskRepository(control_client)
    service = TaskService(repository, ToolRegistry())
    audit = AuditService(repository)
    task = service.create_task("W08 T05 UNKNOWN 收敛不重放")
    task = service.transition(task.task_id, TaskStatus.PLANNING)
    task = service.transition(task.task_id, TaskStatus.EXECUTING)
    return {
        "settings": settings,
        "control_client": control_client,
        "service": service,
        "audit": audit,
        "task": task,
    }


def _tool_calls_count(client: Any) -> int:
    rows = client.fetch_all(TOOL_CALLS_COUNT_SQL, (), max_rows=1)
    return int(rows[0]["total"])


def _controller(stack: dict[str, Any]) -> RecoveryController:
    task = stack["task"]
    return RecoveryController(
        audit=stack["audit"],
        tasks=stack["service"],
        task_id=task.task_id,
        run_id=task.run_id,
    )


def _events(stack: dict[str, Any], task_id: str) -> list[Any]:
    events, _ = stack["audit"].query(
        tenant_id=TENANT_ID, task_id=task_id, limit=200
    )
    return events


def _types(events: list[Any]) -> list[str]:
    return [getattr(event.event_type, "value", event.event_type) for event in events]


def _by_type(events: list[Any], event_type: str) -> list[Any]:
    return [event for event in events if getattr(event.event_type, "value", event.event_type) == event_type]


def _assert_no_replay(
    stack: dict[str, Any], *, before: int, after: int, outcome: Any
) -> None:
    """零增长 + 无工具事件 + 恢复结论自带 delta=0。"""
    assert after == before, f"tool_calls 不得增长：before={before} after={after}"
    assert outcome.tool_calls_delta == 0
    assert outcome.replayed is False

    events = _events(stack, stack["task"].task_id)
    assert not [item for item in _types(events) if item.startswith("TOOL_CALL_")], "恢复器不得产生工具调用事件"

    stopped = _by_type(events, "AGENT_RECOVERY_STOPPED")
    assert len(stopped) == 1
    payload = dict(stopped[0].payload)
    assert payload["tool_calls_delta"] == 0
    assert payload["outcome"] == "ESCALATE"
    assert payload["explanation"].strip()
    assert payload["converged_status"] == outcome.status.value


# ---------------------------------------------------------------------------
# 1) 证据充分 → NEEDS_RECONCILIATION
# ---------------------------------------------------------------------------


def test_unknown_outcome_with_sufficient_evidence_needs_reconciliation(
    recovery_stack,
) -> None:
    stack = recovery_stack
    before = _tool_calls_count(stack["control_client"])

    outcome = _controller(stack).reconcile_unknown(
        action_digest="sha256:" + uuid4().hex,
        idempotency_key=f"idem_{uuid4().hex}",
        target_objects=("dg_lab.seed_orders#2001",),
        evidence_sufficient=True,
    )

    after = _tool_calls_count(stack["control_client"])

    assert outcome.status == "NEEDS_RECONCILIATION"
    assert outcome.status is TaskStatus.NEEDS_RECONCILIATION
    assert outcome.applied is True
    assert outcome.explanation.strip()
    assert stack["service"].get_task(stack["task"].task_id).status is TaskStatus.NEEDS_RECONCILIATION
    _assert_no_replay(stack, before=before, after=after, outcome=outcome)


# ---------------------------------------------------------------------------
# 2) 证据不足 → MANUAL_INTERVENTION
# ---------------------------------------------------------------------------


def test_unknown_outcome_without_evidence_requires_manual_intervention(
    recovery_stack,
) -> None:
    stack = recovery_stack
    # `EXECUTING` 无法直接到 `MANUAL_INTERVENTION`（状态机白名单），先按真实验证
    # 路径推进到 `VERIFYING`，避免用非法迁移伪造结论。
    stack["service"].transition(stack["task"].task_id, TaskStatus.VERIFYING)
    before = _tool_calls_count(stack["control_client"])

    outcome = _controller(stack).reconcile_unknown(
        action_digest="sha256:" + uuid4().hex,
        idempotency_key=f"idem_{uuid4().hex}",
        target_objects=(),
        evidence_sufficient=False,
    )

    after = _tool_calls_count(stack["control_client"])

    assert outcome.status == "MANUAL_INTERVENTION"
    assert outcome.status is TaskStatus.MANUAL_INTERVENTION
    assert outcome.evidence_sufficient is False
    assert outcome.applied is True
    assert outcome.explanation.strip()
    assert stack["service"].get_task(stack["task"].task_id).status is TaskStatus.MANUAL_INTERVENTION
    _assert_no_replay(stack, before=before, after=after, outcome=outcome)
