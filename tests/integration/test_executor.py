"""W03 验收：Tool Registry / Executor / Task Manager 的 MySQL 集成测试。"""

from __future__ import annotations

import sys
import time
import uuid
from typing import Any, Callable

import pytest
from pydantic import BaseModel, ConfigDict

from core.approval.models import ApprovalGrant, issue_grant
from core.approval.prerequisites import PrerequisiteProbe
from core.approval.repository import ApprovalRepository
from core.approval.service import ApprovalService
from core.audit.service import AuditService
from core.config import Settings
from core.errors import DBGuardianError, DatabaseQueryError, StateInvalidError
from core.execution.step_records import StepRecordRepository
from core.security.models import Principal
from core.security.roles import Role
from core.tasks.models import TaskStatus, utc_now
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.contracts import (
    TargetObjectSpec,
    ToolContext,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolSource,
    ToolStatus,
)
from core.tools.executor import ExecutionPolicy, ToolExecutor
from core.tools.registry import ToolRegistry
from database.observers import build_observer_registry
from database.observers.utils import utc_now_iso
from tests.helpers.definitions import write_declarations

pytestmark = pytest.mark.integration

_repo_client = None


def _repo() -> TaskRepository:
    assert _repo_client is not None
    return TaskRepository(_repo_client)


@pytest.fixture(autouse=True)
def _control_repository(control_client, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "_repo_client", control_client)
    yield


@pytest.fixture(autouse=True)
def _cleanup_created_tasks(control_client, monkeypatch):
    """每个用例结束后清理本用例创建的任务及其事件/检查点，避免基线随历史数据漂移。

    清理用 `dg_ctl`（`control_client`）而不是 `admin_client`：本机 `DG_ADMIN_DSN`
    （root）长期报 1045（W06 已登记的既有环境问题），硬依赖管理员账号会让**全部**
    用例在 teardown 阶段 ERROR，从而掩盖真正的实现问题。
    `dg_ctl` 对 `task_events` / `evidence` 没有 DELETE（W06 授权边界），清理不到时
    按既有口径忽略——这两张表的残留不影响本文件的任何断言。
    """
    created: list[str] = []
    original = TaskService.create_task

    def tracked(self, goal, *, risk_level="R0"):
        task = original(self, goal, risk_level=risk_level)
        created.append(task.task_id)
        return task

    monkeypatch.setattr(TaskService, "create_task", tracked)
    yield
    for task_id in created:
        for table in (
            "step_observed",
            "step_prepared",
            "approval_consumptions",
            "approvals",
            "evidence",
            "task_events",
            "tool_calls",
            "task_steps",
            "checkpoints",
            "tasks",
        ):
            try:
                control_client.execute(
                    f"DELETE FROM dg_system.{table} WHERE task_id = %s", (task_id,)
                )
            except DBGuardianError:
                # dg_ctl 对 task_events / evidence 无 DELETE（DatabasePermissionError）：
                # 按 W06 授权边界忽略，残留不影响本文件断言。
                continue


_TARGET_SPECS = [
    TargetObjectSpec(
        object_type="row",
        database_field="database",
        name_field="table",
        keys_field="keys",
    )
]


def _step_records() -> StepRecordRepository:
    """W07 D7：写操作必须落 `step_prepared`，因此执行器必须装配证据仓储。"""
    return StepRecordRepository(_repo().client)


def _write_grant(
    task: Any, tool_name: str, *, tool_version: str = "1.0"
) -> ApprovalGrant:
    """构造一次性凭证（只经 `issue_grant()`；不存在布尔旁路）。"""
    return issue_grant(
        approval_id=f"apr_test_{uuid.uuid4().hex[:8]}",
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        tool_name=tool_name,
        tool_version=tool_version,
        environment_id=task.environment_id,
        action_digest="a" * 64,
        prerequisites_digest="d" * 64,
        policy_version="w06-v1",
        consumed_by="user_worker",
        consumed_at=utc_now(),
    )


def _definition(
    name: str,
    *,
    risk_level: str = "R0",
    side_effect: str = "read",
    permissions: list[str] | None = None,
    timeout_ms: int = 30_000,
    idempotent: bool = True,
    audit_policy: str = "metadata",
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="W03 测试桩工具",
        risk_level=risk_level,
        side_effect=side_effect,
        permissions=permissions or [],
        timeout_ms=timeout_ms,
        idempotent=idempotent,
        audit_policy=audit_policy,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        # P0-03b：写工具必须声明目标对象（读工具留空）。
        target_objects=list(_TARGET_SPECS) if side_effect == "write" else [],
        # W07：写工具还需声明补偿方案与验证计划（构造期 fail closed）。
        **(write_declarations() if side_effect == "write" else {}),
    )


def _source() -> ToolSource:
    return ToolSource(
        database_id="stub",
        db_version="8.0",
        collected_at=utc_now_iso(),
    )


def _tool_result(
    definition: ToolDefinition,
    context: ToolContext,
    *,
    status: ToolStatus = ToolStatus.SUCCESS,
    code: str | None = None,
    message: str = "",
    data: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> ToolResult:
    return ToolResult(
        tool_name=definition.name,
        status=status,
        task_id=context.task_id,
        run_id=context.run_id,
        tool_call_id=context.tool_call_id,
        started_at=utc_now_iso(),
        duration_ms=0.0,
        data=data or {},
        warnings=list(warnings or []),
        error=ToolError(code=code, message=message) if code else None,
        source=_source(),
    )


class StubTool:
    def __init__(
        self,
        definition: ToolDefinition,
        *,
        handler: Callable[[ToolContext, dict[str, Any]], ToolResult] | None = None,
        input_model: type[BaseModel] | None = None,
    ) -> None:
        self.definition = definition
        self.input_model = input_model
        self._handler = handler

    def run(self, context: ToolContext, params: dict[str, Any] | None = None) -> ToolResult:
        if self._handler is not None:
            return self._handler(context, params or {})
        return _tool_result(self.definition, context)


class _LimitParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int


def test_task_creation_and_transition_persist(rw_client) -> None:
    repository = _repo()
    service = TaskService(repository, ToolRegistry())

    task = service.create_task("检查复制延迟", risk_level="R1")
    assert task.status == TaskStatus.PENDING
    assert task.task_id.startswith("task_")
    assert task.run_id.startswith("run_")

    reloaded = _repo().get_task(task.task_id)
    assert reloaded.status == TaskStatus.PENDING

    service.transition(task.task_id, TaskStatus.PLANNING)
    updated = service.transition(task.task_id, TaskStatus.OBSERVING)
    assert updated.status == TaskStatus.OBSERVING
    persisted = _repo().get_task(task.task_id)
    assert persisted.status == TaskStatus.OBSERVING

    events = service.list_events(task.task_id)
    types = {event.event_type for event in events}
    assert {"TASK_CREATED", "TASK_STATE_CHANGED"} <= types
    assert all(
        event.task_id == task.task_id and event.run_id == task.run_id for event in events
    )


def test_pause_resume_cancel_persist(rw_client) -> None:
    repository = _repo()
    service = TaskService(repository, ToolRegistry())
    task = service.create_task("暂停恢复取消")

    paused = service.request_pause(task.task_id)
    assert paused.pause_requested is True

    resumed = service.resume(task.task_id)
    assert resumed.pause_requested is False

    cancelled = service.cancel(task.task_id)
    assert cancelled.status == TaskStatus.CANCELLED
    assert cancelled.cancel_requested is True

    again = _repo().get_task(task.task_id)
    assert again.status == TaskStatus.CANCELLED
    assert again.cancel_requested is True


def test_invalid_transition_rejected(rw_client) -> None:
    repository = _repo()
    service = TaskService(repository, ToolRegistry())

    pending = service.create_task("非法转换：PENDING -> COMPLETED")
    with pytest.raises(StateInvalidError) as pending_error:
        service.transition(pending.task_id, TaskStatus.COMPLETED)
    assert pending_error.value.code.value == "STATE_INVALID"

    completed = service.create_task("非法转换：COMPLETED -> PLANNING")
    for target in (
        TaskStatus.PLANNING,
        TaskStatus.EXECUTING,
        TaskStatus.VERIFYING,
        TaskStatus.COMPLETED,
    ):
        service.transition(completed.task_id, target)

    with pytest.raises(StateInvalidError) as completed_error:
        service.transition(completed.task_id, TaskStatus.PLANNING)
    assert completed_error.value.code.value == "STATE_INVALID"


def test_unknown_tool_denied_and_audited(rw_client, ro_client) -> None:
    repository = _repo()
    service = TaskService(repository, ToolRegistry())
    task = service.create_task("未知工具")
    context = ToolContext(client=ro_client)

    result = service.run_tool(task.task_id, "drop_database", {}, context)
    assert result.status == ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "TOOL_NOT_FOUND"

    record = repository.get_tool_call(result.tool_call_id)
    assert record is not None
    assert record.status == "denied"

    denied_events = [
        event
        for event in repository.list_events(task.task_id)
        if event.event_type == "TOOL_CALL_DENIED"
    ]
    assert denied_events
    assert any(event.tool_call_id == result.tool_call_id for event in denied_events)


def test_invalid_tool_params_denied(rw_client) -> None:
    registry = ToolRegistry()
    registry.register(
        StubTool(
            _definition("stub_limit", permissions=["mysql:read"]),
            input_model=_LimitParams,
        )
    )
    repository = _repo()
    service = TaskService(repository, registry)
    task = service.create_task("非法参数")

    bad_type = service.run_tool(
        task.task_id, "stub_limit", {"limit": "x"}, ToolContext(client=rw_client)
    )
    assert bad_type.status == ToolStatus.DENIED
    assert bad_type.error is not None
    assert bad_type.error.code == "REQUEST_INVALID"

    extra_field = service.run_tool(
        task.task_id,
        "stub_limit",
        {"limit": 10, "oops": True},
        ToolContext(client=rw_client),
    )
    assert extra_field.status == ToolStatus.DENIED
    assert extra_field.error is not None
    assert extra_field.error.code == "REQUEST_INVALID"


def test_missing_permissions_denied(rw_client, ro_client) -> None:
    registry = ToolRegistry()
    registry.register(
        StubTool(_definition("stub_needs_write", permissions=["mysql:write"]))
    )
    repository = _repo()
    service = TaskService(repository, registry)
    task = service.create_task("权限不足")

    result = service.run_tool(
        task.task_id, "stub_needs_write", {}, ToolContext(client=ro_client)
    )
    assert result.status == ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "POLICY_DENIED"
    assert result.error.details["missing_permissions"] == ["mysql:write"]


def _approval_service(registry: ToolRegistry) -> ApprovalService:
    """构造审批服务（控制库仓储 + 服务端前置条件探测）。"""
    repository = _repo()
    return ApprovalService(
        ApprovalRepository(repository.client),
        repository,
        PrerequisiteProbe(repository),
        AuditService(repository),
        settings=Settings(),
        registry=registry,
    )


def test_risk_policy_and_approval(rw_client) -> None:
    registry = ToolRegistry()
    registry.register(StubTool(_definition("stub_r4", risk_level="R4")))
    registry.register(StubTool(_definition("stub_r2", risk_level="R2")))
    repository = _repo()
    service = TaskService(repository, registry)
    task = service.create_task("风险策略")

    r4_result = service.run_tool(
        task.task_id, "stub_r4", {}, ToolContext(client=rw_client)
    )
    assert r4_result.status == ToolStatus.DENIED
    assert r4_result.error is not None
    assert r4_result.error.code == "POLICY_DENIED"

    # ① 无凭证 → APPROVAL_REQUIRED（P0-20：不存在"传 approved=True 即通过"的旁路）
    r2_result = service.run_tool(
        task.task_id, "stub_r2", {}, ToolContext(client=rw_client)
    )
    assert r2_result.status == ToolStatus.DENIED
    assert r2_result.error is not None
    assert r2_result.error.code == "APPROVAL_REQUIRED"

    # ② 凭证不能被直接构造（唯一出口是 ApprovalService.consume）
    with pytest.raises(ValueError):
        ApprovalGrant(
            approval_id="apr_forged",
            tenant_id=task.tenant_id,
            task_id=task.task_id,
            tool_name="stub_r2",
            tool_version="1.0",
            environment_id=task.environment_id,
            action_digest="a" * 64,
            prerequisites_digest="d" * 64,
            policy_version="w06-v1",
            consumed_by="user_worker",
            consumed_at=utc_now(),
        )

    # ③ 成功路径必须使用 consume() 签发的一次性 grant
    approvals = _approval_service(registry)
    operator = Principal(
        user_id="user_operator",
        tenant_id=task.tenant_id,
        roles=(Role.OPERATOR,),
        session_id="sess_operator",
    )
    approver = Principal(
        user_id="user_approver",
        tenant_id=task.tenant_id,
        roles=(Role.APPROVER,),
        session_id="sess_approver",
    )
    worker = Principal(
        user_id="user_worker",
        tenant_id=task.tenant_id,
        roles=(Role.SERVICE_WORKER,),
        session_id="sess_worker",
    )
    detail = approvals.create(
        principal=operator,
        task_id=task.task_id,
        step_id=None,
        tool_name="stub_r2",
        params={},
        target_objects=[],
    )
    approvals.approve(detail.approval_id, principal=approver, reason="LGTM")
    refreshed = service.get_task(task.task_id)
    grant = approvals.consume(
        approval_id=detail.approval_id,
        principal=worker,
        task=refreshed,
        definition=_definition("stub_r2", risk_level="R2"),
        params={},
        environment_row=repository.get_environment(
            refreshed.environment_id, tenant_id=refreshed.tenant_id
        ),
        policy_version=Settings().policy_version,
        envelope_id=None,
        tool_call_id=None,
    )
    approved_result = service.run_tool(
        task.task_id,
        "stub_r2",
        {},
        ToolContext(client=rw_client),
        approval_grant=grant,
    )
    assert approved_result.status == ToolStatus.SUCCESS


def test_tool_timeout_reported(rw_client) -> None:
    definition = _definition("stub_slow", timeout_ms=200)

    def slow_handler(context: ToolContext, params: dict[str, Any]) -> ToolResult:
        time.sleep(0.5)
        return _tool_result(definition, context)

    registry = ToolRegistry()
    registry.register(StubTool(definition, handler=slow_handler))
    repository = _repo()
    executor = ToolExecutor(
        registry, repository, policy=ExecutionPolicy(max_retries=0)
    )
    service = TaskService(repository, registry, executor)
    task = service.create_task("超时")

    result = service.run_tool(
        task.task_id, "stub_slow", {}, ToolContext(client=rw_client)
    )
    assert result.status == ToolStatus.TIMEOUT
    assert result.error is not None
    assert result.error.code == "DB_TIMEOUT"

    timeout_events = [
        event
        for event in repository.list_events(task.task_id)
        if event.event_type == "TOOL_CALL_TIMEOUT"
    ]
    assert timeout_events
    assert timeout_events[0].tool_call_id == result.tool_call_id


def test_idempotent_write_is_replayed(rw_client) -> None:
    counter = {"count": 0}
    definition = _definition("stub_idem_write", side_effect="write", idempotent=True)

    def handler(context: ToolContext, params: dict[str, Any]) -> ToolResult:
        counter["count"] += 1
        return _tool_result(definition, context, data={"affected": 1})

    registry = ToolRegistry()
    registry.register(StubTool(definition, handler=handler))
    repository = _repo()
    service = TaskService(
        repository,
        registry,
        ToolExecutor(registry, repository, step_records=_step_records()),
    )
    task = service.create_task("幂等写")
    key = uuid.uuid4().hex
    grant = _write_grant(task, "stub_idem_write")

    first = service.run_tool(
        task.task_id,
        "stub_idem_write",
        {},
        ToolContext(client=rw_client),
        idempotency_key=key,
        approval_grant=grant,
    )
    assert first.status == ToolStatus.SUCCESS
    assert counter["count"] == 1

    second = service.run_tool(
        task.task_id,
        "stub_idem_write",
        {},
        ToolContext(client=rw_client),
        idempotency_key=key,
        approval_grant=grant,
    )
    assert second.status == ToolStatus.SUCCESS
    assert counter["count"] == 1
    assert "idempotent_replay" in second.warnings
    assert second.tool_call_id == first.tool_call_id
    assert second.task_id == first.task_id
    assert second.run_id == first.run_id


def test_write_timeout_becomes_unknown_outcome_and_needs_reconciliation(rw_client) -> None:
    """P0-17/P0-18：写操作超时不得被解释为失败，也不得自动重试。

    结果未知必须转 `NEEDS_RECONCILIATION`，且事件链里不存在任何 `TOOL_CALL_RETRIED`。
    """
    definition = _definition(
        "stub_slow_write", side_effect="write", idempotent=True, timeout_ms=200
    )

    def slow_handler(context: ToolContext, params: dict[str, Any]) -> ToolResult:
        time.sleep(0.5)
        return _tool_result(definition, context)

    registry = ToolRegistry()
    registry.register(StubTool(definition, handler=slow_handler))
    repository = _repo()
    step_records = _step_records()
    service = TaskService(
        repository,
        registry,
        ToolExecutor(
            registry, repository, policy=ExecutionPolicy(max_retries=0),
            step_records=step_records,
        ),
    )
    task = service.create_task("写操作超时")
    task = service.transition(task.task_id, TaskStatus.PLANNING)
    task = service.transition(task.task_id, TaskStatus.EXECUTING)

    result = service.run_tool(
        task.task_id,
        "stub_slow_write",
        {},
        ToolContext(client=rw_client),
        idempotency_key=uuid.uuid4().hex,
        approval_grant=_write_grant(task, "stub_slow_write"),
    )

    assert result.status is ToolStatus.UNKNOWN_OUTCOME
    assert "write_timeout_outcome_unknown" in result.warnings
    assert service.get_task(task.task_id).status is TaskStatus.NEEDS_RECONCILIATION

    events = [event.event_type for event in repository.list_events(task.task_id)]
    assert "TOOL_CALL_RETRIED" not in events
    assert "TASK_STATE_CHANGED" in events


def test_write_operations_are_not_retried(rw_client) -> None:
    counter = {"count": 0}
    definition = _definition("stub_fail_write", side_effect="write", idempotent=True)

    def handler(context: ToolContext, params: dict[str, Any]) -> ToolResult:
        counter["count"] += 1
        return _tool_result(
            definition,
            context,
            status=ToolStatus.ERROR,
            code="DB_UNAVAILABLE",
            message="数据库暂不可用",
        )

    registry = ToolRegistry()
    registry.register(StubTool(definition, handler=handler))
    repository = _repo()
    executor = ToolExecutor(
        registry, repository, policy=ExecutionPolicy(max_retries=2),
        step_records=_step_records(),
    )
    service = TaskService(repository, registry, executor)
    task = service.create_task("写操作不重试")

    result = service.run_tool(
        task.task_id,
        "stub_fail_write",
        {},
        ToolContext(client=rw_client),
        idempotency_key=uuid.uuid4().hex,
        approval_grant=_write_grant(task, "stub_fail_write"),
    )
    assert result.status == ToolStatus.ERROR
    assert result.error is not None
    assert result.error.code == "DB_UNAVAILABLE"
    assert counter["count"] == 1


def test_readonly_transient_errors_retried_with_backoff(rw_client) -> None:
    counter = {"count": 0}
    delays: list[float] = []
    definition = _definition("stub_flaky_read", side_effect="read", idempotent=True)

    def handler(context: ToolContext, params: dict[str, Any]) -> ToolResult:
        counter["count"] += 1
        if counter["count"] <= 2:
            return _tool_result(
                definition,
                context,
                status=ToolStatus.ERROR,
                code="DB_UNAVAILABLE",
                message="瞬时不可用",
            )
        return _tool_result(definition, context, data={"ok": True})

    registry = ToolRegistry()
    registry.register(StubTool(definition, handler=handler))
    repository = _repo()
    executor = ToolExecutor(
        registry,
        repository,
        policy=ExecutionPolicy(max_retries=2, backoff_base_ms=10, backoff_max_ms=40),
        sleep=delays.append,
    )
    service = TaskService(repository, registry, executor)
    task = service.create_task("只读瞬时错误重试")

    result = service.run_tool(
        task.task_id, "stub_flaky_read", {}, ToolContext(client=rw_client)
    )
    assert result.status == ToolStatus.SUCCESS
    assert counter["count"] == 3
    assert len(delays) == 2
    assert delays[0] == pytest.approx(0.01)
    assert delays[1] == pytest.approx(0.02)


def test_write_after_cancel_is_denied(rw_client) -> None:
    counter = {"count": 0}
    definition = _definition(
        "stub_cancelled_write", side_effect="write", idempotent=True
    )

    def handler(context: ToolContext, params: dict[str, Any]) -> ToolResult:
        counter["count"] += 1
        return _tool_result(definition, context)

    registry = ToolRegistry()
    registry.register(StubTool(definition, handler=handler))
    repository = _repo()
    service = TaskService(repository, registry)
    task = service.create_task("取消后禁止写")

    cancelled = service.cancel(task.task_id)
    assert cancelled.status == TaskStatus.CANCELLED

    result = service.run_tool(
        task.task_id,
        "stub_cancelled_write",
        {},
        ToolContext(client=rw_client),
        idempotency_key=uuid.uuid4().hex,
    )
    assert result.status == ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "POLICY_DENIED"
    assert counter["count"] == 0


def test_write_without_grant_is_denied_before_tool_call(rw_client) -> None:
    """W07 D7：写操作没有 `ApprovalGrant` → `DENIED / APPROVAL_REQUIRED` 且零副作用。

    W06 遗留的"无 grant 只记 warning 然后照常执行"旁路已删除：这里同时断言
    **没有** `step_prepared` 行、工具一次都没被调用。
    """
    counter = {"count": 0}
    definition = _definition("stub_unguarded_write", side_effect="write", idempotent=True)

    def handler(context: ToolContext, params: dict[str, Any]) -> ToolResult:
        counter["count"] += 1
        return _tool_result(definition, context, data={"affected": 1})

    registry = ToolRegistry()
    registry.register(StubTool(definition, handler=handler))
    repository = _repo()
    service = TaskService(
        repository,
        registry,
        ToolExecutor(registry, repository, step_records=_step_records()),
    )
    task = service.create_task("无审批写")

    result = service.run_tool(
        task.task_id,
        "stub_unguarded_write",
        {},
        ToolContext(client=rw_client),
        idempotency_key=uuid.uuid4().hex,
    )

    assert result.status is ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "APPROVAL_REQUIRED"
    assert counter["count"] == 0, "无审批凭据不得调用工具"
    prepared = _repo().client.fetch_all(
        "SELECT prepared_id FROM dg_system.step_prepared WHERE task_id = %s",
        (task.task_id,),
    )
    assert prepared == [], "无审批凭据不得落 step_prepared"


def test_approved_write_always_leaves_step_prepared(rw_client) -> None:
    """W07 D7 的正向不变量：每次真正执行的写调用都先落 `step_prepared`。"""
    definition = _definition("stub_prepared_write", side_effect="write", idempotent=True)

    def handler(context: ToolContext, params: dict[str, Any]) -> ToolResult:
        return _tool_result(definition, context, data={"affected": 1})

    registry = ToolRegistry()
    registry.register(StubTool(definition, handler=handler))
    repository = _repo()
    service = TaskService(
        repository,
        registry,
        ToolExecutor(registry, repository, step_records=_step_records()),
    )
    task = service.create_task("写调用必落证据")
    result = service.run_tool(
        task.task_id,
        "stub_prepared_write",
        {},
        ToolContext(client=rw_client),
        idempotency_key=uuid.uuid4().hex,
        approval_grant=_write_grant(task, "stub_prepared_write"),
    )
    assert result.status is ToolStatus.SUCCESS, result.error
    prepared = _repo().client.fetch_all(
        "SELECT tool_call_id, approval_id FROM dg_system.step_prepared WHERE task_id = %s",
        (task.task_id,),
    )
    assert len(prepared) == 1
    assert prepared[0]["tool_call_id"] == result.tool_call_id
    # 参数台账里出现"无审批写"用例之外的 task，不影响本断言（按 task_id 过滤）。


def test_restart_recovery(rw_client) -> None:
    repository = _repo()
    service = TaskService(repository, ToolRegistry())

    observing = service.create_task("恢复-观察中")
    service.transition(observing.task_id, TaskStatus.PLANNING)
    service.transition(observing.task_id, TaskStatus.OBSERVING)
    service.save_checkpoint(observing.task_id, {"status": "OBSERVING"})

    executing = service.create_task("恢复-执行中")
    service.transition(executing.task_id, TaskStatus.PLANNING)
    service.transition(executing.task_id, TaskStatus.EXECUTING)

    recovered_service = TaskService(_repo(), ToolRegistry())
    recovered = recovered_service.recover_unfinished_tasks()
    by_id = {task.task_id: task for task in recovered}

    assert observing.task_id in by_id
    assert by_id[observing.task_id].status == TaskStatus.OBSERVING

    assert executing.task_id in by_id
    assert by_id[executing.task_id].status == TaskStatus.NEEDS_RECONCILIATION

    observing_events = repository.list_events(observing.task_id)
    assert any(event.event_type == "TASK_RECOVERED" for event in observing_events)
    executing_events = repository.list_events(executing.task_id)
    recovered_events = [
        event for event in executing_events if event.event_type == "TASK_RECOVERED"
    ]
    assert recovered_events
    assert recovered_events[-1].payload["requires_reconciliation"] is True


def test_recovery_handles_more_than_100_unfinished_tasks(rw_client) -> None:
    service = TaskService(_repo(), ToolRegistry())
    created = [service.create_task(f"分页恢复 {index}") for index in range(105)]

    recovered = TaskService(_repo(), ToolRegistry()).recover_unfinished_tasks()
    by_id = {task.task_id: task for task in recovered}
    assert len(recovered) >= 105
    assert all(task.task_id in by_id for task in created)


def test_tool_call_audit_links_task_run_and_call(rw_client, ro_client) -> None:
    registry = build_observer_registry()
    assert len(registry) == 10
    repository = _repo()
    service = TaskService(repository, registry)
    task = service.create_task("审计关联")

    result = service.run_tool(
        task.task_id, "get_db_version", {}, ToolContext(client=ro_client)
    )
    assert result.status == ToolStatus.SUCCESS

    record = repository.get_tool_call(result.tool_call_id)
    assert record is not None
    assert record.task_id == task.task_id
    assert record.run_id == task.run_id
    assert record.status == "success"

    events = repository.list_events(task.task_id)
    started_events = [
        event for event in events if event.event_type == "TOOL_CALL_STARTED"
    ]
    succeeded_events = [
        event for event in events if event.event_type == "TOOL_CALL_SUCCEEDED"
    ]
    assert started_events
    assert succeeded_events
    assert started_events[0].tool_call_id == result.tool_call_id
    assert succeeded_events[0].tool_call_id == result.tool_call_id
