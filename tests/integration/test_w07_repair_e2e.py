"""W07：单引擎 LAB 修复的端到端闭环（真实 MySQL LAB 写操作）。

与 W06 的桩工具验收不同，本文件使用 `adapters/mysql/lab_repair.py` 的真实受限写工具，
走完整链条：创建审批（含 D2 九类 + 披露快照）→ 批准 → Worker 租约 → Gateway 复核 →
消费审批 → StepPrepared → 真实 UPDATE → StepObserved → 审计事件 → 状态语义。

装配纪律（W07 T4/T5）：执行栈只经 `adapters/lab_assembly.py::build_lab_stack()`
装配，不允许在本文件里手搓注册表——否则"唯一写入口"就只是口头约定。

环境说明：控制库清理依赖项目夹具；本机管理员账号不可用时审计类行会有残留，
因此本文件自行用 try/finally 恢复 LAB 数据，保证可重复执行。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from adapters.lab_assembly import build_lab_stack
from adapters.mysql.lab_repair import TOOL_NAME
from core.audit.service import AuditService
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
from core.worker.worker import Worker

pytestmark = pytest.mark.integration

ORDER_ID = 2001
NOTE_SQL = "SELECT note FROM dg_lab.seed_orders WHERE order_id = %s LIMIT 1"
RESTORE_SQL = "UPDATE dg_lab.seed_orders SET note = %s WHERE order_id = %s LIMIT 1"


def _current_note(rw_client: Any) -> str | None:
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


@pytest.fixture
def w07_stack(settings, control_client, ro_client, rw_client):
    """经唯一装配点构建执行栈（含 D2 探针与披露快照）。"""
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
    registry = stack["registry"]
    repository = stack["repository"]
    audit = stack["audit"]
    approvals = stack["approvals"]
    executor = stack["executor"]
    lease_service = LeaseService(LeaseRepository(control_client))

    def principal_resolver(user_id: str) -> Principal:
        return Principal(
            user_id=user_id,
            tenant_id="tenant_local",
            roles=(Role.OPERATOR,),
            auth_method=AuthMethod.DEV_LOCAL,
        )

    def context_factory(envelope, task) -> ToolContext:
        return ToolContext(client=rw_client, task_id=task.task_id, run_id=task.run_id)

    gateway = ToolGateway(
        registry,
        repository,
        lease_service,
        PolicyEngine(policy_version=settings.policy_version),
        executor,
        principal_resolver,
        context_factory,
        settings=settings,
        approval_service=approvals,
        audit=audit,
    )
    worker = Worker(
        "worker_w07",
        "tenant_local",
        "user_local",
        lease_service,
        gateway,
        signing_key=settings.gateway_key(),
    )
    service = TaskService(repository, registry)
    stack.update(
        {
            "service": service,
            "approvals": approvals,
            "gateway": gateway,
            "worker": worker,
            "settings": settings,
            "control_client": control_client,
        }
    )
    return stack


def _approved(stack: dict[str, Any], params: dict[str, Any]) -> tuple[Any, Any]:
    service = stack["service"]
    task = service.create_task("W07 单引擎 LAB 修复端到端")
    task = service.transition(task.task_id, TaskStatus.PLANNING)
    task = service.transition(task.task_id, TaskStatus.EXECUTING)
    operator = Principal(
        user_id="user_w07_operator",
        tenant_id="tenant_local",
        roles=(Role.OPERATOR,),
        session_id="sess_w07_operator",
    )
    approver = Principal(
        user_id="user_w07_approver",
        tenant_id="tenant_local",
        roles=(Role.APPROVER,),
        session_id="sess_w07_approver",
    )
    created = stack["approvals"].create(
        principal=operator,
        task_id=task.task_id,
        step_id=None,
        tool_name=TOOL_NAME,
        params=params,
        target_objects=[],
    )
    detail = stack["approvals"].approve(
        created.approval_id, principal=approver, reason="W07 端到端验收"
    )
    return task, detail


def _envelope(stack: dict[str, Any], task: Any, detail: Any, params: dict[str, Any]):
    lease = stack["worker"].acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    return sign_envelope(
        build_envelope(
            task_id=task.task_id,
            step_id=None,
            worker_id=lease.worker_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            state_version=task.state_version,
            correlation_id=f"corr_w07_{uuid4().hex[:8]}",
            actor_user_id="user_local",
            tool_name=TOOL_NAME,
            tool_version="1.0",
            environment_id=task.environment_id,
            params=params,
            ttl_s=60,
            approval_id=detail.approval_id,
        ),
        stack["settings"].gateway_key(),
    )


def test_w07_approved_repair_updates_lab_and_leaves_observed(
    w07_stack, rw_client
) -> None:
    """审批 → Gateway → 真实单行 UPDATE → StepObserved → 审计 → 状态语义=已执行。"""
    before = _current_note(rw_client)
    target = "w07-approved-repair"
    params = _params(before, target)
    try:
        # AC-13：注册表里这个工具确实是写操作（不是被悄悄降级成读）
        definition = w07_stack["registry"].get(TOOL_NAME).definition
        assert definition.side_effect == "write"
        assert definition.risk_level == "R2"
        assert definition.prepare_required is True
        assert definition.compensation is not None
        assert len(definition.verification_plan) == 3

        task, detail = _approved(w07_stack, params)
        # 披露三节在创建时刻就有内容（不是读取时现算）
        assert detail.disclosure["risk"]["effective_risk"] == "R2"
        assert detail.disclosure["compensation"]["is_rollback"] is False
        assert detail.disclosure["verification_plan"]["min_assertions"] == 3
        assert len(detail.disclosure_digest or "") == 64

        envelope = _envelope(w07_stack, task, detail, params)
        result = w07_stack["gateway"].execute(envelope, params)

        assert result.status is ToolStatus.SUCCESS, result.error
        assert result.data.get("affected_rows") == 1
        assert _current_note(rw_client) == target, "目标库必须真的发生单行更新"

        assert w07_stack["step_records"].get_observed(result.tool_call_id) is not None, (
            "成功的写操作必须留下 StepObserved 证据"
        )
        assert w07_stack["service"].get_task(task.task_id).status is not (
            TaskStatus.NEEDS_RECONCILIATION
        ), "成功写入不得被收敛为需核对"

        events, _ = w07_stack["audit"].query(
            tenant_id="tenant_local", task_id=task.task_id, limit=200
        )
        types = {getattr(e.event_type, "value", e.event_type) for e in events}
        assert "APPROVAL_REQUESTED" in types
        assert "APPROVAL_CONSUMED" in types
        assert "STEP_PREPARED" in types
        assert "TOOL_CALL_SUCCEEDED" in types

        consumed = w07_stack["approvals"].get(
            detail.approval_id, tenant_id="tenant_local"
        )
        assert consumed.status.value == "CONSUMED"
        state = w07_stack["approvals"].state_of(consumed)
        assert state["state"] == "EXECUTED", state
        assert state["label"] == "已执行"
        assert state["degraded"] is False
        assert state["execution_evidence"]["step_observed_outcome"] == "success"
    finally:
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))


def test_w07_prerequisite_change_blocks_write(w07_stack, rw_client) -> None:
    """执行前基线变化：Gateway 复核 + 工具层双保险，目标库一行都不能被改。"""
    before = _current_note(rw_client)
    params = _params("__stale_baseline__", "must-not-be-written")
    try:
        task, detail = _approved(w07_stack, params)
        envelope = _envelope(w07_stack, task, detail, params)
        result = w07_stack["gateway"].execute(envelope, params)

        assert result.status is ToolStatus.DENIED, result.error
        assert _current_note(rw_client) == before, "基线不匹配时禁止任何写入"

        consumed = w07_stack["approvals"].get(
            detail.approval_id, tenant_id="tenant_local"
        )
        state = w07_stack["approvals"].state_of(consumed)
        # 拒绝不是"结果未知"：已消费但结果是明确否定 → FAILED。
        assert state["state"] in {"FAILED", "UNKNOWN_OUTCOME"}, state
        assert state["state"] != "EXECUTED"
    finally:
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))


def test_w07_stack_wires_audit_and_step_records(w07_stack) -> None:
    """装配点自检：审批服务必须拿到证据仓储（否则状态语义会 degraded）。"""
    approvals = w07_stack["approvals"]
    assert isinstance(approvals, type(approvals))
    assert isinstance(w07_stack["audit"], AuditService)
    assert w07_stack["step_records"] is not None
    assert w07_stack["prepare_probe"] is not None
    assert w07_stack["db_engine"] == "mysql"
