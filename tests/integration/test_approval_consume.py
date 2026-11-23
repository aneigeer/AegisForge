"""R2 审批消费接入唯一执行链（P0-20，T2/T3/T4/T7）。

覆盖：
- N=8 并发消费 → 恰好 1 成功、其余 `APPROVAL_ALREADY_CONSUMED`；
- 无 `approval_id` 的 R2 → `APPROVAL_REQUIRED` 且 `tool_calls` 计数为 0；
- 过期 / 撤销 / 已消费重放 / 参数篡改 四类拒绝，且不产生工具调用；
- T2 双通道：参数变更 → `channel=parameters`；目标对象抽取规则变更 →
  `channel=target_objects`；工具版本变更 → 审批失效；
- 审计不可用 → R2 被阻断，工具未被调用。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from core.approval.models import ApprovalStatus
from core.approval.prerequisites import PrerequisiteProbe
from core.approval.repository import ApprovalRepository
from core.approval.service import ApprovalService
from core.audit.service import AuditService
from core.errors import (
    ApprovalAlreadyConsumedError,
    ApprovalDigestMismatchError,
    ApprovalExpiredError,
    AuditUnavailableError,
    DatabaseQueryError,
)
from core.gateway.envelope import build_envelope, sign_envelope
from core.gateway.leases import LeaseRepository, LeaseService
from core.gateway.tool_gateway import ToolGateway
from core.security.models import AuthMethod, ExecutionEnvelope, Principal, WorkerLease
from core.security.policy import PolicyEngine
from core.security.roles import Role
from core.tasks.models import Task, utc_now
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.contracts import (
    TargetObjectSpec,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolSource,
    ToolStatus,
)
from core.tools.executor import ToolExecutor
from core.tools.registry import ToolRegistry
from core.worker.worker import Worker
from database.observers.utils import utc_now_iso
from tests.helpers.definitions import write_declarations

pytestmark = pytest.mark.integration

TOOL_NAME = "stub_r2_write"
PARAMS: dict[str, Any] = {
    "database": "dg_lab",
    "table": "orders",
    "keys": ["id=1001"],
    "status": "PAID",
}

_CLEANUP_TABLES = (
    "step_observed",
    "step_prepared",
    "approval_consumptions",
    "approvals",
    "task_events",
    "tool_calls",
    "task_steps",
    "checkpoints",
    "execution_envelopes",
    "worker_leases",
    "tasks",
)


def _spec(
    *,
    name_field: str = "table",
    keys_field: str | None = "keys",
    required: bool = True,
) -> TargetObjectSpec:
    return TargetObjectSpec(
        object_type="row",
        database_field="database",
        name_field=name_field,
        keys_field=keys_field,
        required=required,
    )


def _definition(
    *,
    version: str = "1.0",
    risk_level: str = "R2",
    name_field: str = "table",
    keys_field: str | None = "keys",
    required: bool = True,
) -> ToolDefinition:
    return ToolDefinition(
        name=TOOL_NAME,
        description="R2 写工具测试桩（仅存在于测试代码）",
        version=version,
        risk_level=risk_level,  # type: ignore[arg-type]
        side_effect="write",
        permissions=["mysql:write"],
        required_role="dg_rw",
        idempotent=False,
        audit_policy="metadata",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        db_engines=["mysql"],
        allowed_environments=["LAB"],
        operation_class="repair",
        target_objects=[
            _spec(name_field=name_field, keys_field=keys_field, required=required)
        ],
        # W07：写工具还需声明补偿方案与验证计划（构造期 fail closed）。
        **write_declarations(),
    )


class StubTool:
    """记录调用次数的桩工具：用于断言"工具未被调用"。"""

    def __init__(self, definition: ToolDefinition, counter: dict[str, int]) -> None:
        self.definition = definition
        self.input_model = None
        self._counter = counter

    def run(self, context: ToolContext, params: dict[str, Any] | None = None) -> ToolResult:
        self._counter["calls"] = self._counter.get("calls", 0) + 1
        return ToolResult(
            tool_name=self.definition.name,
            status=ToolStatus.SUCCESS,
            task_id=context.task_id,
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            started_at=utc_now_iso(),
            duration_ms=0.0,
            data={"affected": 1},
            warnings=[],
            error=None,
            source=ToolSource(database_id="stub", db_version="8.0", collected_at=utc_now_iso()),
        )


@pytest.fixture(autouse=True)
def _cleanup_created_tasks(admin_client, monkeypatch):
    created: list[str] = []
    original = TaskService.create_task

    def tracked(self, goal, **kwargs):
        task = original(self, goal, **kwargs)
        created.append(task.task_id)
        return task

    monkeypatch.setattr(TaskService, "create_task", tracked)
    yield
    for task_id in created:
        for table in _CLEANUP_TABLES:
            admin_client.execute(
                f"DELETE FROM dg_system.{table} WHERE task_id = %s", (task_id,)
            )


@pytest.fixture
def counter() -> dict[str, int]:
    return {"calls": 0}


@pytest.fixture
def registry(counter: dict[str, int]) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(StubTool(_definition(), counter))
    return registry


@pytest.fixture
def repository(control_client) -> TaskRepository:
    return TaskRepository(control_client)


@pytest.fixture
def audit(repository: TaskRepository) -> AuditService:
    return AuditService(repository)


@pytest.fixture
def approvals(repository: TaskRepository, audit: AuditService, registry: ToolRegistry, settings):
    return ApprovalService(
        ApprovalRepository(repository.client),
        repository,
        PrerequisiteProbe(repository),
        audit,
        settings=settings,
        registry=registry,
    )


@pytest.fixture
def service(repository: TaskRepository, registry: ToolRegistry) -> TaskService:
    return TaskService(repository, registry)


@pytest.fixture
def lease_service(control_client) -> LeaseService:
    return LeaseService(LeaseRepository(control_client))


@pytest.fixture
def gateway(
    settings,
    repository: TaskRepository,
    registry: ToolRegistry,
    lease_service: LeaseService,
    approvals: ApprovalService,
    audit: AuditService,
    rw_client,
) -> ToolGateway:
    step_records = __import__("core.execution.step_records", fromlist=["StepRecordRepository"])
    executor = ToolExecutor(
        registry,
        repository,
        step_records=step_records.StepRecordRepository(repository.client, audit=audit),
        audit=audit,
    )

    def principal_resolver(user_id: str) -> Principal:
        return Principal(
            user_id=user_id,
            tenant_id="tenant_local",
            roles=(Role.OPERATOR,),
            auth_method=AuthMethod.DEV_LOCAL,
        )

    def context_factory(envelope: ExecutionEnvelope, task: Task) -> ToolContext:
        return ToolContext(client=rw_client, task_id=task.task_id, run_id=task.run_id)

    return ToolGateway(
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


@pytest.fixture
def worker_factory(settings, lease_service: LeaseService, gateway: ToolGateway):
    def factory(worker_id: str = "worker_a", actor_user_id: str = "user_local") -> Worker:
        return Worker(
            worker_id,
            "tenant_local",
            actor_user_id,
            lease_service,
            gateway,
            signing_key=settings.gateway_key(),
        )

    return factory


@pytest.fixture
def operator() -> Principal:
    return Principal(
        user_id="user_operator",
        tenant_id="tenant_local",
        roles=(Role.OPERATOR,),
        session_id="sess_operator",
    )


@pytest.fixture
def approver() -> Principal:
    return Principal(
        user_id="user_approver",
        tenant_id="tenant_local",
        roles=(Role.APPROVER,),
        session_id="sess_approver",
    )


@pytest.fixture
def worker_principal() -> Principal:
    return Principal(
        user_id="user_worker",
        tenant_id="tenant_local",
        roles=(Role.SERVICE_WORKER,),
        session_id="sess_worker",
    )


def _create_approval(approvals: ApprovalService, task: Task, operator: Principal, **overrides):
    kwargs: dict[str, Any] = {
        "principal": operator,
        "task_id": task.task_id,
        "step_id": None,
        "tool_name": TOOL_NAME,
        "params": dict(PARAMS),
        "target_objects": [],
    }
    kwargs.update(overrides)
    return approvals.create(**kwargs)


def _approved(approvals: ApprovalService, task: Task, operator: Principal, approver: Principal):
    detail = _create_approval(approvals, task, operator)
    return approvals.approve(detail.approval_id, principal=approver, reason="LGTM")


def _envelope(
    settings,
    lease: WorkerLease,
    task: Task,
    *,
    approval_id: str | None,
    params: dict[str, Any] | None = None,
    tool_version: str = "1.0",
) -> ExecutionEnvelope:
    envelope = build_envelope(
        task_id=task.task_id,
        step_id=None,
        worker_id=lease.worker_id,
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        state_version=task.state_version,
        correlation_id="corr_approval_consume",
        actor_user_id="user_local",
        tool_name=TOOL_NAME,
        tool_version=tool_version,
        environment_id=task.environment_id,
        params=params if params is not None else dict(PARAMS),
        ttl_s=60,
        approval_id=approval_id,
    )
    return sign_envelope(envelope, settings.gateway_key())


def _tool_call_count(client, task_id: str) -> int:
    rows = client.fetch_all(
        "SELECT COUNT(*) AS total FROM dg_system.tool_calls WHERE task_id = %s", (task_id,)
    )
    return int(rows[0]["total"]) if rows else 0


def _consume(approvals, task, principal, approval_id, *, definition=None, params=None, policy_version="w06-v1"):
    return approvals.consume(
        approval_id=approval_id,
        principal=principal,
        task=task,
        definition=definition or _definition(),
        params=PARAMS if params is None else params,
        environment_row=_environments_row(approvals, task),
        policy_version=policy_version,
        envelope_id="env_test",
        tool_call_id=None,
    )


def _environments_row(approvals, task) -> dict[str, Any]:
    return approvals._tasks.get_environment(  # noqa: SLF001 - 复用服务端事实来源
        task.environment_id, tenant_id=task.tenant_id
    )


# ---------------------------------------------------------------------------
# T3：并发消费恰好一个成功
# ---------------------------------------------------------------------------


def test_concurrent_consume_has_exactly_one_winner(
    approvals, service, control_client, operator, approver, worker_principal
) -> None:
    task = service.create_task("并发消费同一审批")
    detail = _approved(approvals, task, operator, approver)
    refreshed = service.get_task(task.task_id)

    barrier = threading.Barrier(8)
    results: list[Any] = []
    errors: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait(timeout=30)
        try:
            grant = _consume(approvals, refreshed, worker_principal, detail.approval_id)
            with lock:
                results.append(grant)
        except Exception as exc:  # noqa: BLE001 - 并发下按错误码分类统计
            with lock:
                errors.append(getattr(exc, "code", type(exc).__name__).value
                              if hasattr(getattr(exc, "code", None), "value")
                              else type(exc).__name__)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert len(results) == 1, f"并发消费应恰好 1 个成功，实际 {len(results)}；错误={errors}"
    assert errors.count("APPROVAL_ALREADY_CONSUMED") == 7, errors
    assert approvals.get(detail.approval_id, tenant_id="tenant_local").status is ApprovalStatus.CONSUMED
    rows = control_client.fetch_all(
        "SELECT COUNT(*) AS total FROM dg_system.approval_consumptions WHERE approval_id = %s",
        (detail.approval_id,),
    )
    assert int(rows[0]["total"]) == 1


# ---------------------------------------------------------------------------
# 执行链接入
# ---------------------------------------------------------------------------


def test_r2_without_approval_id_is_denied_without_tool_calls(
    service, gateway, worker_factory, settings, control_client, counter
) -> None:
    task = service.create_task("缺少 approval_id 的 R2")
    worker = worker_factory("worker_a")
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    result = gateway.execute(_envelope(settings, lease, task, approval_id=None), dict(PARAMS))

    assert result.status is ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "APPROVAL_REQUIRED"
    assert counter.get("calls", 0) == 0
    assert _tool_call_count(control_client, task.task_id) == 0


def test_r2_with_approval_executes_once_and_records_evidence(
    service,
    gateway,
    worker_factory,
    approvals,
    operator,
    approver,
    settings,
    control_client,
    counter,
    audit,
) -> None:
    task = service.create_task("带审批的 R2 执行")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    worker = worker_factory("worker_a")
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    result = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
    )

    assert result.status is ToolStatus.SUCCESS, result.error
    assert counter["calls"] == 1
    assert approvals.get(detail.approval_id, tenant_id="tenant_local").status is ApprovalStatus.CONSUMED

    prepared = control_client.fetch_all(
        "SELECT tool_call_id, approval_id, action_digest FROM dg_system.step_prepared "
        "WHERE task_id = %s",
        (task.task_id,),
    )
    assert len(prepared) == 1
    assert prepared[0]["approval_id"] == detail.approval_id
    assert prepared[0]["action_digest"] == detail.action_digest

    events = {event.event_type for event in audit.query(tenant_id="tenant_local", task_id=task.task_id, limit=200)[0]}
    assert "STEP_PREPARED" in events
    assert "APPROVAL_CONSUMED" in events

    # 同一张审批不能被第二次使用（新信封、新 nonce）
    second = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
    )
    assert second.status is ToolStatus.DENIED
    assert second.error is not None
    assert second.error.code == "APPROVAL_ALREADY_CONSUMED"
    assert counter["calls"] == 1


def test_expired_approval_is_denied_at_gateway(
    service, gateway, worker_factory, approvals, operator, approver, settings, control_client, counter
) -> None:
    task = service.create_task("过期审批")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    control_client.execute(
        "UPDATE dg_system.approvals SET expires_at = %s WHERE approval_id = %s",
        (past.replace(tzinfo=None), detail.approval_id),
    )
    worker = worker_factory("worker_a")
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    result = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
    )
    assert result.status is ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "APPROVAL_EXPIRED"
    assert counter.get("calls", 0) == 0
    assert _tool_call_count(control_client, task.task_id) == 0


def test_revoked_approval_is_denied_at_gateway(
    service, gateway, worker_factory, approvals, operator, approver, settings, counter
) -> None:
    task = service.create_task("撤销审批")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    approvals.revoke(detail.approval_id, principal=operator, reason="不做了")
    worker = worker_factory("worker_a")
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    result = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
    )
    assert result.status is ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "APPROVAL_REVOKED"
    assert counter.get("calls", 0) == 0


# ---------------------------------------------------------------------------
# T2 双通道：参数变更 / 目标对象抽取规则变更 / 工具版本变更
# ---------------------------------------------------------------------------


def test_parameter_change_is_reported_as_parameters_channel(
    approvals, service, operator, approver, worker_principal
) -> None:
    task = service.create_task("参数篡改")
    detail = _approved(approvals, task, operator, approver)
    tampered = {**PARAMS, "status": "REFUNDED"}

    with pytest.raises(ApprovalDigestMismatchError) as excinfo:
        _consume(approvals, task, worker_principal, detail.approval_id, params=tampered)
    assert excinfo.value.details["channel"] == "parameters"


def test_target_object_rule_change_is_reported_as_target_objects_channel(
    approvals, service, operator, approver, worker_principal
) -> None:
    """同样的参数、不同的目标对象抽取规则 → 参数摘要不变，但目标对象摘要变了。"""
    task = service.create_task("目标对象规则变更")
    detail = _approved(approvals, task, operator, approver)

    # 规则改为"不抽取 keys"，同样的参数会得到 keys=[] → 目标对象集合不同
    drifted = _definition(keys_field=None)
    with pytest.raises(ApprovalDigestMismatchError) as excinfo:
        _consume(
            approvals,
            task,
            worker_principal,
            detail.approval_id,
            definition=drifted,
        )
    assert excinfo.value.details["channel"] == "target_objects"


def test_extraction_rule_drift_blocks_execution_without_tool_call(
    service,
    gateway,
    worker_factory,
    approvals,
    operator,
    approver,
    settings,
    control_client,
    counter,
    monkeypatch,
) -> None:
    """抽取规则原地漂移（换 name_field、tool_version 不变）→ 消费被拒、工具未被调用。

    这是真实缺口：`parameters` 没变（`parameters_digest` 通过），只有目标对象映射变了，
    因此必须靠消费侧重算 `target_objects` 才能发现。
    """
    task = service.create_task("抽取规则漂移")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    worker = worker_factory("worker_a")
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    # 注册表里把 spec 的 name_field 原地改成 target_table（版本号不变）
    drifted_tool = StubTool(_definition(name_field="target_table", required=False), counter)
    gateway._registry._tools[TOOL_NAME] = drifted_tool  # noqa: SLF001

    result = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
    )

    assert result.status is ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "APPROVAL_DIGEST_MISMATCH"
    assert result.error.details["channel"] == "target_objects"
    assert result.error.details["reason"] == "EXTRACTED_TARGETS_EMPTY"
    assert counter.get("calls", 0) == 0
    assert _tool_call_count(control_client, task.task_id) == 0


def test_consume_denial_is_audited_with_channel(
    approvals, service, operator, approver, worker_principal, audit, control_client
) -> None:
    """消费被拒必须留下 `APPROVAL_CONSUME_DENIED` 事件（channel 可定位）且无工具调用。"""
    task = service.create_task("拒绝可追溯")
    detail = _approved(approvals, task, operator, approver)

    with pytest.raises(ApprovalDigestMismatchError):
        _consume(
            approvals,
            task,
            worker_principal,
            detail.approval_id,
            params={**PARAMS, "status": "REFUNDED"},
        )

    events, _ = audit.query(
        tenant_id="tenant_local",
        task_id=task.task_id,
        event_types=["APPROVAL_CONSUME_DENIED"],
        limit=10,
    )
    assert len(events) == 1
    payload = events[0].payload
    assert payload["channel"] == "parameters"
    assert payload["deny_code"] == "APPROVAL_DIGEST_MISMATCH"
    assert payload["approval_id"] == detail.approval_id
    assert payload["actor_user_id"] == "user_worker"
    assert "trace_id" in payload
    assert _tool_call_count(control_client, task.task_id) == 0


def test_tool_version_change_invalidates_approval(
    approvals, service, operator, approver, worker_principal
) -> None:
    task = service.create_task("工具版本变更")
    detail = _approved(approvals, task, operator, approver)

    with pytest.raises(ApprovalDigestMismatchError) as excinfo:
        _consume(
            approvals,
            task,
            worker_principal,
            detail.approval_id,
            definition=_definition(version="1.1"),
        )
    assert excinfo.value.details["channel"] == "tool_version"


def test_already_consumed_approval_cannot_be_reused(
    approvals, service, operator, approver, worker_principal
) -> None:
    task = service.create_task("重复消费")
    detail = _approved(approvals, task, operator, approver)
    _consume(approvals, task, worker_principal, detail.approval_id)
    with pytest.raises(ApprovalAlreadyConsumedError):
        _consume(approvals, task, worker_principal, detail.approval_id)


def test_expired_approval_consume_marks_status(
    approvals, service, operator, approver, worker_principal, control_client
) -> None:
    task = service.create_task("过期标记")
    detail = _approved(approvals, task, operator, approver)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    control_client.execute(
        "UPDATE dg_system.approvals SET expires_at = %s WHERE approval_id = %s",
        (past.replace(tzinfo=None), detail.approval_id),
    )
    with pytest.raises(ApprovalExpiredError):
        _consume(approvals, task, worker_principal, detail.approval_id)
    assert approvals.get(detail.approval_id, tenant_id="tenant_local").status is ApprovalStatus.EXPIRED


# ---------------------------------------------------------------------------
# T7：审计不可用 → R2 阻断
# ---------------------------------------------------------------------------


def test_audit_unavailable_blocks_r2_before_tool_call(
    service,
    gateway,
    worker_factory,
    approvals,
    operator,
    approver,
    settings,
    control_client,
    counter,
    monkeypatch,
) -> None:
    task = service.create_task("审计不可用阻断")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    worker = worker_factory("worker_a")
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    def _broken_emit(event, **kwargs):  # noqa: ANN001, ANN003 - 模拟账本不可写
        raise AuditUnavailableError("模拟审计写入失败")

    monkeypatch.setattr(gateway._audit, "emit_required", _broken_emit)  # noqa: SLF001

    with pytest.raises(AuditUnavailableError):
        gateway.execute(
            _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
        )

    assert counter.get("calls", 0) == 0
    assert _tool_call_count(control_client, task.task_id) == 0
    # 审批已被消费，但操作未执行——这是"至多一次消费、不保证恰好执行一次"的已知边界
    assert approvals.get(detail.approval_id, tenant_id="tenant_local").status is ApprovalStatus.CONSUMED


def test_prepared_write_failure_blocks_execution(
    service,
    gateway,
    worker_factory,
    approvals,
    operator,
    approver,
    settings,
    counter,
    monkeypatch,
) -> None:
    """`insert_prepared` 是 fail closed 落点：写不进去就不得发起执行（P0-16）。"""
    task = service.create_task("Prepared 落盘失败")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    worker = worker_factory("worker_a")
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    step_records = gateway._executor._step_records  # noqa: SLF001

    class _BrokenClient:
        """仅实现证据仓储用到的方法，execute 一律失败（模拟证据表不可写）。"""

        def execute(self, *args: Any, **kwargs: Any) -> int:
            raise DatabaseQueryError("模拟证据表不可写")

        def fetch_all(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return []

    # 用"坏客户端"重建仓储：让真实的 insert_prepared 失败路径（→ AUDIT_UNAVAILABLE）生效
    broken = type(step_records)(_BrokenClient())  # type: ignore[arg-type]
    gateway._executor._step_records = broken  # noqa: SLF001

    result = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
    )
    assert result.status is ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "AUDIT_UNAVAILABLE"
    assert counter.get("calls", 0) == 0
