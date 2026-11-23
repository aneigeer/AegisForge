"""W08 T04 / AC-10：补偿是**独立审批**的 R2 动作（真实 MySQL LAB，端到端）。

链路：审批 → Gateway → 真实单行 UPDATE（`note` 变成 after）→ 发起补偿
（新 `approval_id` + `compensation_of` + 参数互换）→ 批准 → Gateway → UPDATE
（`note` 回到 before）→ `step_observed` → `COMPENSATION_EXECUTED`。

另外两条不变量：
- 用**原** `approval_id` 驱动补偿 → 被 Gateway 拒绝（`APPROVAL_ALREADY_CONSUMED`），
  目标行 `note` 逐字不变；
- 同一原审批重复发起补偿 → 被 `uq_approvals_compensation_of` 挡住，
  留 `COMPENSATION_DENIED`，不给目标行留下任何待批单。

装配纪律：执行栈只经 `adapters/lab_assembly.py::build_lab_stack()` 装配。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from adapters.lab_assembly import build_lab_stack
from adapters.mysql.lab_repair import TOOL_NAME
from core.approval.models import ApprovalDetail, ApprovalStatus
from core.errors import StateConflictError
from core.gateway.envelope import build_envelope, sign_envelope
from core.gateway.leases import LeaseRepository, LeaseService
from core.gateway.tool_gateway import ToolGateway
from core.security.canonical import canonical_digest
from core.security.models import AuthMethod, Principal
from core.security.policy import PolicyEngine
from core.security.roles import Role
from core.tasks.models import TaskStatus
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.contracts import ToolContext, ToolStatus
from core.verification.compensation import CompensationService
from core.worker.worker import Worker

pytestmark = pytest.mark.integration

ORDER_ID = 2001
TENANT_ID = "tenant_local"
NOTE_SQL = "SELECT note FROM dg_lab.seed_orders WHERE order_id = %s LIMIT 1"
RESTORE_SQL = "UPDATE dg_lab.seed_orders SET note = %s WHERE order_id = %s LIMIT 1"


def _note(rw_client: Any) -> str | None:
    rows = rw_client.fetch_all(NOTE_SQL, (ORDER_ID,))
    if not rows:
        pytest.skip("LAB 种子数据 seed_orders 不存在，请先执行 bootstrap_mysql.py")
    return rows[0].get("note")


def _params(expected_note: str | None, note: str) -> dict[str, Any]:
    return {
        "database": "dg_lab",
        "table": "seed_orders",
        "order_id": ORDER_ID,
        "keys": f"order_id={ORDER_ID}",
        "note": note,
        "expected_note": expected_note,
    }


def _operator() -> Principal:
    return Principal(
        user_id="user_w08_operator",
        tenant_id=TENANT_ID,
        roles=(Role.OPERATOR,),
        session_id="sess_w08_operator",
        auth_method=AuthMethod.DEV_LOCAL,
    )


def _approver() -> Principal:
    return Principal(
        user_id="user_w08_approver",
        tenant_id=TENANT_ID,
        roles=(Role.APPROVER,),
        session_id="sess_w08_approver",
        auth_method=AuthMethod.DEV_LOCAL,
    )


@pytest.fixture
def w08_stack(settings, control_client, ro_client, rw_client):
    """真实执行栈 + 补偿服务（只经唯一装配点）。"""
    repository = TaskRepository(control_client)
    environment_row = repository.get_environment(
        settings.default_environment_id, tenant_id=settings.default_tenant_id
    )
    if environment_row is None:
        pytest.skip("默认 MySQL 环境不存在，请先执行 bootstrap_mysql.py")
    stack = build_lab_stack(
        settings=settings,
        control_client=control_client,
        environment_row=environment_row,
        ro_client=ro_client,
        rw_client=rw_client,
    )
    lease_service = LeaseService(LeaseRepository(control_client))

    def principal_resolver(user_id: str) -> Principal:
        return Principal(
            user_id=user_id,
            tenant_id=TENANT_ID,
            roles=(Role.OPERATOR,),
            auth_method=AuthMethod.DEV_LOCAL,
        )

    def context_factory(envelope, task) -> ToolContext:
        return ToolContext(client=rw_client, task_id=task.task_id, run_id=task.run_id)

    gateway = ToolGateway(
        stack["registry"],
        stack["repository"],
        lease_service,
        PolicyEngine(policy_version=settings.policy_version),
        stack["executor"],
        principal_resolver,
        context_factory,
        settings=settings,
        approval_service=stack["approvals"],
        audit=stack["audit"],
    )
    worker = Worker(
        "worker_w08_comp",
        TENANT_ID,
        "user_local",
        lease_service,
        gateway,
        signing_key=settings.gateway_key(),
    )
    stack.update(
        {
            "service": TaskService(stack["repository"], stack["registry"]),
            "gateway": gateway,
            "worker": worker,
            "settings": settings,
            "compensation": CompensationService(
                approvals=stack["approvals"], audit=stack["audit"], tasks=stack["repository"]
            ),
        }
    )
    return stack


def _executing_task(stack: dict[str, Any]) -> Any:
    service = stack["service"]
    task = service.create_task("W08 补偿独立审批链")
    task = service.transition(task.task_id, TaskStatus.PLANNING)
    return service.transition(task.task_id, TaskStatus.EXECUTING)


def _approved(
    stack: dict[str, Any], task: Any, params: dict[str, Any]
) -> ApprovalDetail:
    created = stack["approvals"].create(
        principal=_operator(),
        task_id=task.task_id,
        step_id=None,
        tool_name=TOOL_NAME,
        params=params,
        target_objects=[],
    )
    return stack["approvals"].approve(
        created.approval_id, principal=_approver(), reason="W08 补偿验收"
    )


def _execute(
    stack: dict[str, Any], task: Any, detail: ApprovalDetail, params: dict[str, Any], lease
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
            correlation_id=f"corr_w08_comp_{uuid4().hex[:8]}",
            actor_user_id="user_local",
            tool_name=TOOL_NAME,
            tool_version="1.0",
            environment_id=current.environment_id,
            params=params,
            ttl_s=60,
            approval_id=detail.approval_id,
        ),
        stack["settings"].gateway_key(),
    )
    return stack["gateway"].execute(envelope, params)


def _events(stack: dict[str, Any], task_id: str) -> list[Any]:
    events, _ = stack["audit"].query(tenant_id=TENANT_ID, task_id=task_id, limit=300)
    return events


def _by_type(events: list[Any], event_type: str) -> list[Any]:
    return [
        event
        for event in events
        if getattr(event.event_type, "value", event.event_type) == event_type
    ]


def test_compensation_is_independent_approval_and_restores_note(
    w08_stack, rw_client
) -> None:
    """原动作 after → 补偿审批（PENDING/compensation_of）→ 批准 → 执行 → 回到 before。"""
    stack = w08_stack
    before = "w08-comp-before"
    after = "w08-comp-after"
    rw_client.execute(RESTORE_SQL, (before, ORDER_ID))
    try:
        task = _executing_task(stack)
        original_detail = _approved(stack, task, _params(before, after))
        lease = stack["worker"].acquire_lease(
            task.task_id, task.environment_id, ttl_s=60
        )

        executed = _execute(stack, task, original_detail, _params(before, after), lease)
        assert executed.status is ToolStatus.SUCCESS, executed.error
        assert _note(rw_client) == after, "原动作必须真的把 note 改成 after"

        repository = stack["approvals"].repository
        original = repository.get(original_detail.approval_id, tenant_id=TENANT_ID)
        assert original.status is ApprovalStatus.CONSUMED

        compensation = stack["compensation"].request(
            original_approval=original, before=before, after=after, principal=_operator()
        )

        # 补偿是独立审批：新 id、PENDING、compensation_of 指向原审批。
        assert compensation.approval_id != original.approval_id
        assert compensation.status is ApprovalStatus.PENDING
        assert compensation.action_digest != original.action_digest
        assert (
            repository.get_compensation_of(compensation.approval_id)
            == original.approval_id
        )
        # 补偿参数 = 原参数互换 note / expected_note，其余逐字沿用。
        comp_params = compensation.action_descriptor["parameters"]
        assert comp_params["note"] == before
        assert comp_params["expected_note"] == after
        assert comp_params["keys"] == f"order_id={ORDER_ID}"

        # 同一原审批重复发起 → 唯一键挡住，且留 COMPENSATION_DENIED。
        with pytest.raises(StateConflictError) as duplicate:
            stack["compensation"].request(
                original_approval=original, before=before, after=after, principal=_operator()
            )
        assert duplicate.value.details["reason"] == "COMPENSATION_ALREADY_REQUESTED"

        approved = stack["compensation"].approve(
            compensation.approval_id, principal=_approver(), reason="W08 补偿批准"
        )
        assert approved.status.value == "APPROVED"

        compensated = _execute(stack, task, approved, comp_params, lease)
        assert compensated.status is ToolStatus.SUCCESS, compensated.error
        assert _note(rw_client) == before, "补偿后目标行必须逐字回到 before"

        stack["compensation"].record_executed(
            compensation_approval_id=compensation.approval_id,
            tool_call_id=compensated.tool_call_id,
            observed_digest=canonical_digest(compensated.data or {}),
            outcome="success",
            principal=_operator(),
        )

        events = _events(stack, task.task_id)
        requested = _by_type(events, "COMPENSATION_REQUESTED")
        approved_events = _by_type(events, "COMPENSATION_APPROVED")
        executed_events = _by_type(events, "COMPENSATION_EXECUTED")
        denied = _by_type(events, "COMPENSATION_DENIED")

        assert len(requested) == 1
        assert len(approved_events) == 1
        assert len(executed_events) == 1
        assert len(denied) == 1, [event.payload for event in denied]

        assert requested[0].payload["original_approval_id"] == original.approval_id
        assert (
            requested[0].payload["compensation_approval_id"]
            == compensation.approval_id
        )
        assert requested[0].payload["requested_by"] == "user_w08_operator"
        assert approved_events[0].payload["approver_id"] == "user_w08_approver"
        assert (
            approved_events[0].payload["original_approval_id"] == original.approval_id
        )
        assert executed_events[0].payload["tool_call_id"] == compensated.tool_call_id
        assert executed_events[0].payload["outcome"] == "success"
        assert denied[0].payload["original_approval_id"] == original.approval_id
        assert denied[0].payload["deny_code"]
    finally:
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))


def test_driving_compensation_with_original_approval_is_denied(
    w08_stack, rw_client
) -> None:
    """复用原 `approval_id` 驱动补偿 → 拒绝，且目标行 `note` 逐字不变。"""
    stack = w08_stack
    before = "w08-comp-before"
    after = "w08-comp-after"
    rw_client.execute(RESTORE_SQL, (before, ORDER_ID))
    try:
        task = _executing_task(stack)
        original_detail = _approved(stack, task, _params(before, after))
        lease = stack["worker"].acquire_lease(
            task.task_id, task.environment_id, ttl_s=60
        )
        executed = _execute(stack, task, original_detail, _params(before, after), lease)
        assert executed.status is ToolStatus.SUCCESS, executed.error
        assert _note(rw_client) == after

        # 用原审批 + 原参数再驱动一次（等价于"把原审批当补偿审批用"）。
        reused = _execute(stack, task, original_detail, _params(before, after), lease)

        assert reused.status is ToolStatus.DENIED, reused.status
        assert reused.error is not None
        assert "APPROVAL_ALREADY_CONSUMED" in str(reused.error.code)
        assert _note(rw_client) == after, "被拒绝的补偿不得改动目标行"

        repository = stack["approvals"].repository
        original = repository.get(original_detail.approval_id, tenant_id=TENANT_ID)
        assert repository.get_compensation_of(original.approval_id) is None
        assert original.action_digest
    finally:
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))
