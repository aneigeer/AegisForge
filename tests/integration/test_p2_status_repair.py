"""P2-T3 集成测试：LAB 订单状态修复（前进）+ 补偿（独立审批的反向动作）。

链路（真实 MySQL LAB，端到端）：

1. `mysql_lab_advance_order_status`：审批 → Gateway → 单行 UPDATE（pending→paid）
   → 目标行读回校验；
2. 补偿是**另一个** R2 工具 `mysql_lab_revert_order_status`：新 approval_id +
   `compensation_of` + 参数互换 → 批准 → 执行（paid→pending）；
3. 用**原** approval 驱动反向动作 → 被 Gateway 拒绝（`APPROVAL_ALREADY_CONSUMED`），
   目标行状态逐字不变；
4. preflight 拒绝（基线漂移）**不消耗审批**：审批仍为 PENDING。

装配纪律：执行栈只经 `adapters/lab_assembly.py::build_lab_stack()` 装配。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from adapters.lab_assembly import build_lab_stack
from adapters.mysql.lab_status_repair import ADVANCE_TOOL_NAME, REVERT_TOOL_NAME
from core.approval.models import ApprovalDetail, ApprovalStatus
from core.gateway.envelope import build_envelope, sign_envelope
from core.gateway.leases import LeaseRepository, LeaseService
from core.gateway.tool_gateway import ToolGateway
from core.security.models import AuthMethod, Principal
from core.security.policy import PolicyEngine
from core.security.roles import Role
from core.tasks.models import TaskStatus
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.contracts import ToolContext, ToolStatus

pytestmark = pytest.mark.integration

ORDER_ID = 2001
TENANT_ID = "tenant_local"

STATUS_SQL = "SELECT status FROM dg_lab.seed_orders WHERE order_id = %s LIMIT 1"
RESET_SQL = "UPDATE dg_lab.seed_orders SET status = %s WHERE order_id = %s LIMIT 1"


def _status(rw_client: Any) -> str | None:
    rows = rw_client.fetch_all(STATUS_SQL, (ORDER_ID,))
    if not rows:
        pytest.skip("LAB 种子数据 seed_orders 不存在，请先执行 bootstrap_mysql.py")
    return rows[0].get("status")


def _reset(rw_client: Any, status: str) -> None:
    rw_client.execute(RESET_SQL, (status, ORDER_ID))


def _params(expected: str, target: str) -> dict[str, Any]:
    return {
        "database": "dg_lab",
        "table": "seed_orders",
        "order_id": ORDER_ID,
        "keys": f"order_id={ORDER_ID}",
        "expected_status": expected,
        "target_status": target,
    }


def _operator() -> Principal:
    return Principal(
        user_id="user_local",
        tenant_id=TENANT_ID,
        roles=(Role.OPERATOR,),
        auth_method=AuthMethod.DEV_LOCAL,
    )


def _approver() -> Principal:
    return Principal(
        user_id="user_approver",
        tenant_id=TENANT_ID,
        roles=(Role.APPROVER,),
        auth_method=AuthMethod.DEV_LOCAL,
    )


@pytest.fixture()
def stack(settings, control_client, ro_client, rw_client) -> dict[str, Any]:
    repository = TaskRepository(control_client)
    environment_row = repository.get_environment(
        settings.default_environment_id, tenant_id=settings.default_tenant_id
    )
    if environment_row is None:
        pytest.skip("默认 MySQL 环境不存在，请先执行 bootstrap_mysql.py")
    built = build_lab_stack(
        settings=settings,
        control_client=control_client,
        environment_row=environment_row,
        ro_client=ro_client,
        rw_client=rw_client,
    )
    lease_service = LeaseService(LeaseRepository(control_client))
    gateway = ToolGateway(
        built["registry"],
        built["repository"],
        lease_service,
        PolicyEngine(policy_version=settings.policy_version),
        built["executor"],
        lambda actor_user_id: _operator(),
        lambda envelope, task: ToolContext(
            client=rw_client, task_id=task.task_id, run_id=task.run_id
        ),
        settings=settings,
        approval_service=built["approvals"],
        audit=built["audit"],
    )
    built.update(
        {
            "service": TaskService(built["repository"], built["registry"]),
            "gateway": gateway,
            "lease_service": lease_service,
            "settings": settings,
            "rw": rw_client,
        }
    )
    return built


def _executing_task(stack: dict[str, Any]) -> Any:
    service = stack["service"]
    task = service.create_task("P2-T3 状态修复与补偿")
    task = service.transition(task.task_id, TaskStatus.PLANNING)
    return service.transition(task.task_id, TaskStatus.EXECUTING)


def _approved(
    stack: dict[str, Any],
    task: Any,
    *,
    tool_name: str,
    params: dict[str, Any],
    compensation_of: str | None = None,
) -> ApprovalDetail:
    created = stack["approvals"].create(
        principal=_operator(),
        task_id=task.task_id,
        step_id=None,
        tool_name=tool_name,
        params=params,
        target_objects=[],
        compensation_of=compensation_of,
    )
    return stack["approvals"].approve(
        created.approval_id, principal=_approver(), reason="P2-T3 状态修复验收"
    )


def _execute(
    stack: dict[str, Any],
    task: Any,
    *,
    tool_name: str,
    params: dict[str, Any],
    approval_id: str,
    lease: Any,
) -> Any:
    current = stack["service"].get_task(task.task_id)
    envelope = sign_envelope(
        build_envelope(
            task_id=current.task_id,
            step_id=None,
            worker_id=lease.worker_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            state_version=current.state_version,
            correlation_id=f"corr_p2_status_{uuid4().hex[:8]}",
            actor_user_id="user_local",
            tool_name=tool_name,
            tool_version="1.0",
            environment_id=current.environment_id,
            params=params,
            ttl_s=60,
            approval_id=approval_id,
        ),
        stack["settings"].gateway_key(),
    )
    return stack["gateway"].execute(envelope, params)


def test_advance_then_revert_with_independent_approvals(stack, rw_client) -> None:
    _reset(rw_client, "pending")
    task = _executing_task(stack)
    lease = stack["lease_service"].acquire(
        worker_id="worker_p2_status",
        task_id=task.task_id,
        tenant_id=TENANT_ID,
        environment_id=task.environment_id,
        ttl_s=60,
    )

    forward = _params("pending", "paid")
    forward_approval = _approved(stack, task, tool_name=ADVANCE_TOOL_NAME, params=forward)
    executed = _execute(
        stack,
        task,
        tool_name=ADVANCE_TOOL_NAME,
        params=forward,
        approval_id=forward_approval.approval_id,
        lease=lease,
    )
    assert executed.status is ToolStatus.SUCCESS, executed.error
    assert _status(rw_client) == "paid"
    assert executed.data["before"] == "pending" and executed.data["after"] == "paid"

    # 补偿：另一个工具 + 新审批 + compensation_of 指向原审批。
    backward = _params("paid", "pending")
    revert_approval = _approved(
        stack,
        task,
        tool_name=REVERT_TOOL_NAME,
        params=backward,
        compensation_of=forward_approval.approval_id,
    )
    assert revert_approval.approval_id != forward_approval.approval_id

    restored = _execute(
        stack,
        task,
        tool_name=REVERT_TOOL_NAME,
        params=backward,
        approval_id=revert_approval.approval_id,
        lease=lease,
    )
    assert restored.status is ToolStatus.SUCCESS, restored.error
    assert _status(rw_client) == "pending", "补偿必须把状态写回执行前"


def test_revert_with_original_approval_is_denied(stack, rw_client) -> None:
    _reset(rw_client, "pending")
    task = _executing_task(stack)
    lease = stack["lease_service"].acquire(
        worker_id="worker_p2_status",
        task_id=task.task_id,
        tenant_id=TENANT_ID,
        environment_id=task.environment_id,
        ttl_s=60,
    )
    forward = _params("pending", "paid")
    approval = _approved(stack, task, tool_name=ADVANCE_TOOL_NAME, params=forward)
    assert (
        _execute(
            stack,
            task,
            tool_name=ADVANCE_TOOL_NAME,
            params=forward,
            approval_id=approval.approval_id,
            lease=lease,
        ).status
        is ToolStatus.SUCCESS
    )

    # 复用原审批驱动反向动作 → 必须被拒，且目标行保持不变。
    reused = _execute(
        stack,
        task,
        tool_name=REVERT_TOOL_NAME,
        params=_params("paid", "pending"),
        approval_id=approval.approval_id,
        lease=lease,
    )
    assert reused.status is ToolStatus.DENIED
    assert reused.error is not None
    assert "APPROVAL_ALREADY_CONSUMED" in str(reused.error.code)
    assert _status(rw_client) == "paid", "被拒的补偿不得改动目标行"


def test_preflight_denial_does_not_consume_approval(stack, rw_client) -> None:
    _reset(rw_client, "cancelled")
    task = _executing_task(stack)
    lease = stack["lease_service"].acquire(
        worker_id="worker_p2_status",
        task_id=task.task_id,
        tenant_id=TENANT_ID,
        environment_id=task.environment_id,
        ttl_s=60,
    )
    # 审批针对 pending→paid，但库里已是 cancelled → preflight 命中 0 行。
    approval = _approved(
        stack, task, tool_name=ADVANCE_TOOL_NAME, params=_params("pending", "paid")
    )
    denied = _execute(
        stack,
        task,
        tool_name=ADVANCE_TOOL_NAME,
        params=_params("pending", "paid"),
        approval_id=approval.approval_id,
        lease=lease,
    )
    assert denied.status is ToolStatus.DENIED
    assert denied.error is not None and denied.error.code == "PREFLIGHT_NO_TARGET"
    assert _status(rw_client) == "cancelled"

    detail = stack["approvals"].repository.get(approval.approval_id, tenant_id=TENANT_ID)
    assert detail.status is ApprovalStatus.APPROVED, "预检拒绝不得消耗审批"
