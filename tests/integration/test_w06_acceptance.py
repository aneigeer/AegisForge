"""W06 独立验收用例（QA 侧，不复用工程师用例）。

设计原则：**先看断言，再问"如果实现是假的，这个测试还会不会绿"**。
因此本文件的每一条用例都刻意换一条与工程师用例不同的触发路径：

- T3：8 个线程各自持有**独立连接 + 独立 ApprovalService 实例**，且每个线程在进入
  并发消费前都已从自己的连接读到 `APPROVED`（覆盖"两个线程都读到 APPROVED 后才各自
  UPDATE"的时序）；另加一条**绕开应用层先查后写**的用例，直接证明"单次消费"由
  `approval_consumptions` 的唯一键保证（P0-07）。
- T4：四类拒绝全部走**真实 Gateway 全链路**（而不是直接调 `consume()`），
  这样"工具未被调用"才有意义（`tool_calls` 行数与桩工具计数双断言）。
- T2：目标对象通道刻意构造"**两个 spec 不同、`tool_version` 相同、参数完全相同**"
  的 definition，并在断言里显式校验 `drift.version == approved.version`，
  防止用例退化成"只改了库内快照"。
- T5：用 `dg_ctl` 直连控制库，对 `task_events` 执行 UPDATE / DELETE / DROP、
  对 `approvals` 执行 DELETE，并**额外核对 live grants**（不看脚本，看数据库里真实的授权）。
- T7：不用 `emit_required` 造失败，而是让 `TaskRepository.insert_event` 对
  `STEP_PREPARED` 这一条事件失败，证明"8c 门禁"本身是阻断点。
- T8：走 Gateway + 真实超时工具，验证 `UNKNOWN_OUTCOME` → `NEEDS_RECONCILIATION`
  且无 `TOOL_CALL_RETRIED`。
- P0-16：独立验证"写操作成功后确有 `step_observed` 证据"，并验证 `Reconciler`
  不会把一次**成功**的写入误判为"需要核对"。

本文件不修改任何源码；失败的用例在 `docs/w06/04-test-report.md` 里区分
"源码缺陷"与"测试缺陷"。
"""

from __future__ import annotations

import subprocess
import threading
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.security import get_principal as security_get_principal
from core.approval.models import ApprovalStatus
from core.approval.prerequisites import PrerequisiteProbe
from core.approval.repository import ApprovalRepository, duplicate_key
from core.approval.service import ApprovalService
from core.audit.events import AuditEventType, make_event
from core.audit.integrity import verify_event_chain
from core.audit.service import AuditService
from core.errors import (
    AuditUnavailableError,
    DBGuardianError,
    DatabasePermissionError,
    DatabaseQueryError,
)
from core.execution.reconciler import Reconciler
from core.execution.step_records import StepRecordRepository
from core.gateway.envelope import build_envelope, sign_envelope
from core.gateway.leases import LeaseRepository, LeaseService
from core.gateway.tool_gateway import ToolGateway
from core.security.models import AuthMethod, ExecutionEnvelope, Principal, WorkerLease
from core.security.policy import PolicyEngine
from core.security.roles import Role
from core.tasks.models import Task, TaskStatus, utc_now
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
from database.mysql.connection import MysqlClient
from database.mysql.transaction import transaction
from database.observers.utils import utc_now_iso
from tests.helpers.definitions import write_declarations

pytestmark = pytest.mark.integration

TOOL_NAME = "qa_acceptance_r2_write"
PARAMS: dict[str, Any] = {
    "database": "dg_lab",
    "table": "orders",
    "keys": ["id=1001"],
    "status": "PAID",
}

# 逆 FK 顺序清理（新表必须排在 task_events 之前，否则外键失败）
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


# ---------------------------------------------------------------------------
# 工具桩与定义
# ---------------------------------------------------------------------------


def _spec(*, name_field: str = "table", keys_field: str | None = "keys") -> TargetObjectSpec:
    return TargetObjectSpec(
        object_type="row",
        database_field="database",
        name_field=name_field,
        keys_field=keys_field,
    )


def _definition(
    *,
    version: str = "1.0",
    name_field: str = "table",
    keys_field: str | None = "keys",
    timeout_ms: int = 5000,
) -> ToolDefinition:
    return ToolDefinition(
        name=TOOL_NAME,
        description="QA 验收用 R2 写工具桩（仅存在于测试代码）",
        version=version,
        risk_level="R2",
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
        timeout_ms=timeout_ms,
        target_objects=[_spec(name_field=name_field, keys_field=keys_field)],
        # W07：写工具还需声明补偿方案与验证计划（构造期 fail closed）。
        **write_declarations(),
    )


class StubTool:
    """记录调用次数的桩工具；`delay_s` 用于制造写操作超时（T8）。"""

    def __init__(
        self,
        definition: ToolDefinition,
        counter: dict[str, int],
        *,
        delay_s: float = 0.0,
    ) -> None:
        self.definition = definition
        self.input_model = None
        self._counter = counter
        self._delay_s = delay_s

    def run(self, context: ToolContext, params: dict[str, Any] | None = None) -> ToolResult:
        self._counter["calls"] = self._counter.get("calls", 0) + 1
        if self._delay_s:
            time.sleep(self._delay_s)
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


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def provision_client(admin_client, control_client) -> MysqlClient:
    """建/删租户与环境用。

    优先管理员账号；管理员凭据不可用时退回 `dg_ctl`（它对 `tenants`/`environments`
    有 INSERT/DELETE）。**审计/审批类只追加表的清理只有管理员能做**，
    因此降级模式下会留残留行并发出告警（见测试报告"环境限制"一节）。
    """
    try:
        admin_client.fetch_all("SELECT 1")
    except DBGuardianError as exc:
        warnings.warn(
            f"管理员账号不可用（{exc.code.value}），清理退化为 dg_ctl 尽力模式；"
            "审批/审计类行不会被删除",
            RuntimeWarning,
            stacklevel=2,
        )
        return control_client
    return admin_client


def _cleanup_task_rows(client: MysqlClient, task_id: str) -> list[str]:
    """按逆 FK 顺序尽力清理；返回未能删除的表名（授权不允许时）。"""
    blocked: list[str] = []
    for table in _CLEANUP_TABLES:
        try:
            client.execute(f"DELETE FROM dg_system.{table} WHERE task_id = %s", (task_id,))
        except DBGuardianError:
            blocked.append(table)
    return blocked


@pytest.fixture(autouse=True)
def _cleanup_created_tasks(provision_client, monkeypatch):
    created: list[str] = []
    original = TaskService.create_task

    def tracked(self, goal, **kwargs):  # noqa: ANN001, ANN003 - 追踪本用例创建的任务
        task = original(self, goal, **kwargs)
        created.append(task.task_id)
        return task

    monkeypatch.setattr(TaskService, "create_task", tracked)
    yield
    for task_id in created:
        _cleanup_task_rows(provision_client, task_id)


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
def approvals(
    repository: TaskRepository, audit: AuditService, registry: ToolRegistry, settings
) -> ApprovalService:
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
def step_records(repository: TaskRepository, audit: AuditService) -> StepRecordRepository:
    return StepRecordRepository(repository.client, audit=audit)


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
    step_records: StepRecordRepository,
    rw_client,
) -> ToolGateway:
    executor = ToolExecutor(registry, repository, step_records=step_records, audit=audit)

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
    def factory(worker_id: str = "worker_qa") -> Worker:
        return Worker(
            worker_id,
            "tenant_local",
            "user_local",
            lease_service,
            gateway,
            signing_key=settings.gateway_key(),
        )

    return factory


@pytest.fixture
def operator() -> Principal:
    return Principal(
        user_id="user_qa_operator",
        tenant_id="tenant_local",
        roles=(Role.OPERATOR,),
        session_id="sess_qa_operator",
    )


@pytest.fixture
def approver() -> Principal:
    return Principal(
        user_id="user_qa_approver",
        tenant_id="tenant_local",
        roles=(Role.APPROVER,),
        session_id="sess_qa_approver",
    )


@pytest.fixture
def worker_principal() -> Principal:
    return Principal(
        user_id="user_qa_worker",
        tenant_id="tenant_local",
        roles=(Role.SERVICE_WORKER,),
        session_id="sess_qa_worker",
    )


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _approved(approvals: ApprovalService, task: Task, operator: Principal, approver: Principal):
    detail = approvals.create(
        principal=operator,
        task_id=task.task_id,
        step_id=None,
        tool_name=TOOL_NAME,
        params=dict(PARAMS),
        target_objects=[],
    )
    return approvals.approve(detail.approval_id, principal=approver, reason="QA 验收")


def _envelope(
    settings,
    lease: WorkerLease,
    task: Task,
    *,
    approval_id: str | None,
    params: dict[str, Any] | None = None,
    tool_version: str = "1.0",
) -> ExecutionEnvelope:
    return sign_envelope(
        build_envelope(
            task_id=task.task_id,
            step_id=None,
            worker_id=lease.worker_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            state_version=task.state_version,
            correlation_id=f"corr_qa_{uuid4().hex[:8]}",
            actor_user_id="user_local",
            tool_name=TOOL_NAME,
            tool_version=tool_version,
            environment_id=task.environment_id,
            params=params if params is not None else dict(PARAMS),
            ttl_s=60,
            approval_id=approval_id,
        ),
        settings.gateway_key(),
    )


def _tool_call_count(client, task_id: str) -> int:
    rows = client.fetch_all(
        "SELECT COUNT(*) AS total FROM dg_system.tool_calls WHERE task_id = %s", (task_id,)
    )
    return int(rows[0]["total"]) if rows else 0


def _environment_row(repository: TaskRepository, task: Task) -> dict[str, Any]:
    row = repository.get_environment(task.environment_id, tenant_id=task.tenant_id)
    assert row is not None
    return dict(row)


def _event_types(audit: AuditService, task_id: str) -> list[str]:
    events, _ = audit.query(tenant_id="tenant_local", task_id=task_id, limit=200)
    return [event.event_type for event in events]


def _new_control_client(settings) -> MysqlClient:
    """每个并发线程独立的控制库连接（不复用 session 级 fixture）。"""
    return MysqlClient(
        settings.control_mysql_dsn(),
        database_id="dg_system",
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )


# ===========================================================================
# T3｜并发消费只有一个成功
# ===========================================================================


def test_t3_eight_independent_connections_consume_exactly_once(
    approvals,
    service,
    settings,
    registry,
    control_client,
    operator,
    approver,
    worker_principal,
) -> None:
    """8 个线程、8 条独立连接、8 个独立服务实例，同时消费同一张 `APPROVED` 审批。

    每个线程在进入并发点**之前**都已从自己的连接读到 `status='APPROVED'`
    （即"双方都先读到 APPROVED，再各自尝试 UPDATE"的时序），
    因此本用例能证伪"应用层先查后写"式的假实现。
    """
    task = service.create_task("T3 独立连接并发消费")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    refreshed = service.get_task(task.task_id)
    environment_row = _environment_row(service.repository, refreshed)

    workers = 8
    barrier = threading.Barrier(workers)
    lock = threading.Lock()
    approvals_seen: list[str] = []
    successes: list[str] = []
    failures: list[str] = []

    def attempt() -> None:
        client = _new_control_client(settings)
        task_repository = TaskRepository(client)
        svc = ApprovalService(
            ApprovalRepository(client),
            task_repository,
            PrerequisiteProbe(task_repository),
            AuditService(task_repository),
            settings=settings,
            registry=registry,
        )
        pre = client.fetch_all(
            "SELECT status FROM dg_system.approvals WHERE approval_id = %s",
            (detail.approval_id,),
        )
        with lock:
            approvals_seen.append(str(pre[0]["status"]))
        barrier.wait(timeout=30)
        try:
            grant = svc.consume(
                approval_id=detail.approval_id,
                principal=worker_principal,
                task=refreshed,
                definition=_definition(),
                params=dict(PARAMS),
                environment_row=environment_row,
                policy_version="w06-v1",
                envelope_id=f"env_qa_{uuid4().hex[:8]}",
                tool_call_id=None,
            )
            with lock:
                successes.append(grant.approval_id)
        except Exception as exc:  # noqa: BLE001 - 并发下按错误码分类统计
            code = getattr(exc, "code", None)
            with lock:
                failures.append(code.value if hasattr(code, "value") else type(exc).__name__)

    threads = [threading.Thread(target=attempt) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    # 时序前提：8 个线程都先看到 APPROVED 才进入竞争
    assert approvals_seen == ["APPROVED"] * workers, approvals_seen
    assert len(successes) == 1, f"应恰好 1 个成功，实际 {len(successes)}；失败={failures}"
    assert failures.count("APPROVAL_ALREADY_CONSUMED") == workers - 1, failures

    rows = control_client.fetch_all(
        "SELECT COUNT(*) AS total FROM dg_system.approval_consumptions WHERE approval_id = %s",
        (detail.approval_id,),
    )
    assert int(rows[0]["total"]) == 1
    assert approvals.get(detail.approval_id, tenant_id="tenant_local").status is ApprovalStatus.CONSUMED


def test_t3_unique_key_alone_prevents_double_consumption(
    approvals, service, settings, control_client, operator, approver
) -> None:
    """P0-07：把"应用层先查后写"整个去掉，只靠唯一键也必须只成功一次。

    两个独立连接各自读到 `APPROVED`（**不加行锁**，模拟应用层校验失效），
    然后各自插入消费台账 —— 第二次必须撞唯一键（1062）。
    """
    task = service.create_task("T3 唯一键兜底")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)

    rows = control_client.fetch_all(
        "SELECT status, approval_id, tenant_id, task_id, nonce_hash, action_digest, "
        "policy_version FROM dg_system.approvals WHERE approval_id = %s",
        (detail.approval_id,),
    )
    assert rows, "审批记录必须存在"
    assert str(rows[0]["status"]) == "APPROVED"
    approval_row = rows[0]

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        client = _new_control_client(settings)
        repository = ApprovalRepository(client)
        # 先做一次**无锁**快照读：两个线程都会看到 APPROVED（这是本用例的前提）
        snapshot = client.fetch_all(
            "SELECT status FROM dg_system.approvals WHERE approval_id = %s",
            (detail.approval_id,),
        )
        assert str(snapshot[0]["status"]) == "APPROVED"
        barrier.wait(timeout=30)
        try:
            with transaction(client) as conn:
                repository.insert_consumption(
                    conn,
                    consumption_id=f"acn_{uuid4().hex[:20]}",
                    approval_id=str(approval_row["approval_id"]),
                    tenant_id=str(approval_row["tenant_id"]),
                    task_id=str(approval_row["task_id"]),
                    nonce_hash=str(approval_row["nonce_hash"]),
                    action_digest=str(approval_row["action_digest"]),
                    policy_version=str(approval_row["policy_version"]),
                    consumed_by="user_qa_worker",
                    consumed_at=utc_now(),
                    tool_call_id=None,
                    envelope_id=None,
                )
            with lock:
                outcomes.append("OK")
        except DatabaseQueryError as exc:
            with lock:
                outcomes.append("DUP" if duplicate_key(exc) else f"ERR:{exc.details}")

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(outcomes) == ["DUP", "OK"], outcomes
    total = control_client.fetch_all(
        "SELECT COUNT(*) AS total FROM dg_system.approval_consumptions WHERE approval_id = %s",
        (detail.approval_id,),
    )
    assert int(total[0]["total"]) == 1


# ===========================================================================
# T4｜过期 / 撤销 / 重放 / 参数篡改：四类拒绝且 tool_calls 计数为 0
# ===========================================================================


def test_t4_expired_approval_is_rejected_without_tool_call(
    service, gateway, worker_factory, approvals, operator, approver, settings, control_client, counter
) -> None:
    task = service.create_task("T4 过期")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    control_client.execute(
        "UPDATE dg_system.approvals SET expires_at = %s WHERE approval_id = %s",
        (past.replace(tzinfo=None), detail.approval_id),
    )
    worker = worker_factory()
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    result = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
    )

    assert result.status is ToolStatus.DENIED
    assert result.error is not None and result.error.code == "APPROVAL_EXPIRED"
    assert counter.get("calls", 0) == 0
    assert _tool_call_count(control_client, task.task_id) == 0
    # 过期是"审批状态"，必须显式落库（不是只返回一个错误码）
    assert approvals.get(detail.approval_id, tenant_id="tenant_local").status is ApprovalStatus.EXPIRED


def test_t4_revoked_approval_is_rejected_without_tool_call(
    service, gateway, worker_factory, approvals, operator, approver, settings, control_client, counter
) -> None:
    task = service.create_task("T4 撤销")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    approvals.revoke(detail.approval_id, principal=operator, reason="QA 撤销")
    worker = worker_factory()
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    result = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
    )

    assert result.status is ToolStatus.DENIED
    assert result.error is not None and result.error.code == "APPROVAL_REVOKED"
    assert counter.get("calls", 0) == 0
    assert _tool_call_count(control_client, task.task_id) == 0


def test_t4_replay_of_consumed_approval_is_rejected_without_new_tool_call(
    service,
    gateway,
    worker_factory,
    approvals,
    operator,
    approver,
    settings,
    control_client,
    counter,
) -> None:
    task = service.create_task("T4 重放")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    worker = worker_factory()
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    first = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
    )
    assert first.status is ToolStatus.SUCCESS, first.error
    assert counter["calls"] == 1
    calls_after_first = _tool_call_count(control_client, task.task_id)

    # 新信封、新 nonce，只复用 approval_id —— 必须被拒
    replay = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
    )

    assert replay.status is ToolStatus.DENIED
    assert replay.error is not None and replay.error.code == "APPROVAL_ALREADY_CONSUMED"
    assert counter["calls"] == 1, "重放不得再次调用工具"
    assert _tool_call_count(control_client, task.task_id) == calls_after_first


def test_t4_tampered_parameters_are_rejected_without_tool_call(
    service, gateway, worker_factory, approvals, operator, approver, settings, control_client, counter
) -> None:
    task = service.create_task("T4 参数篡改")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    worker = worker_factory()
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    tampered = {**PARAMS, "status": "REFUNDED"}

    result = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id, params=tampered),
        tampered,
    )

    assert result.status is ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "APPROVAL_DIGEST_MISMATCH"
    assert result.error.details["channel"] == "parameters"
    assert counter.get("calls", 0) == 0
    assert _tool_call_count(control_client, task.task_id) == 0


# ===========================================================================
# T2｜双通道：parameters / target_objects
# ===========================================================================


def test_t2_parameter_change_hits_parameters_channel_via_gateway(
    service, gateway, worker_factory, approvals, operator, approver, settings, counter
) -> None:
    task = service.create_task("T2 参数通道")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    worker = worker_factory()
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    tampered = {**PARAMS, "keys": ["id=2002"]}

    result = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id, params=tampered),
        tampered,
    )

    assert result.status is ToolStatus.DENIED
    assert result.error is not None
    assert result.error.details["channel"] == "parameters"
    assert counter.get("calls", 0) == 0


def test_t2_extraction_rule_drift_hits_target_objects_channel_via_gateway(
    service,
    gateway,
    worker_factory,
    approvals,
    operator,
    approver,
    settings,
    control_client,
    counter,
) -> None:
    """抽取规则原地漂移、`tool_version` 不变、参数不变 → `channel=target_objects`。

    漂移方式是"**抽取仍然成功，但抽出的对象不同**"（`name_field` 由 `table` 改为
    `database`，于是同一份参数抽出 `name=dg_lab` 而不是 `name=orders`）——
    这是最严格的一种：`parameters_digest` 不会变，只有目标对象通道能发现。

    为了证明用例没有退化成"改库内快照"，这里显式断言：
    ① 漂移后的 definition 与审批登记的工具版本**完全相同**；
    ② 传给 Gateway 的参数与审批时**逐键相同**。
    """
    task = service.create_task("T2 目标对象通道")
    approved_definition = _definition(version="1.0")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    assert detail.tool_version == approved_definition.version
    worker = worker_factory()
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    drifted_definition = _definition(version="1.0", name_field="database")
    assert drifted_definition.version == approved_definition.version
    assert drifted_definition.target_objects != approved_definition.target_objects

    # 只替换注册表里的契约（同一 ToolRegistry 实例，Gateway 与 Approvals 共用）
    gateway._registry._tools[TOOL_NAME] = StubTool(drifted_definition, counter)  # noqa: SLF001

    result = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
    )

    assert result.status is ToolStatus.DENIED, result
    assert result.error is not None
    assert result.error.code == "APPROVAL_DIGEST_MISMATCH"
    assert result.error.details["channel"] == "target_objects", result.error.details
    assert counter.get("calls", 0) == 0
    assert _tool_call_count(control_client, task.task_id) == 0
    # 审批未被消费（被拒的消费不消耗审批）
    assert approvals.get(detail.approval_id, tenant_id="tenant_local").status is ApprovalStatus.APPROVED


def test_t2_extraction_rule_drift_to_empty_targets_is_still_target_objects(
    service,
    gateway,
    worker_factory,
    approvals,
    operator,
    approver,
    settings,
    control_client,
    counter,
) -> None:
    """漂移导致**抽不出**目标对象（`required=False` + 字段不存在）时也要落到目标对象通道。"""
    task = service.create_task("T2 目标对象通道（空抽取）")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    worker = worker_factory()
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    drifted = ToolDefinition(
        **{
            **_definition(version="1.0").model_dump(),
            "target_objects": [
                TargetObjectSpec(
                    object_type="row",
                    database_field="database",
                    name_field="target_table",
                    keys_field="keys",
                    required=False,
                )
            ],
        }
    )
    assert drifted.version == "1.0"
    gateway._registry._tools[TOOL_NAME] = StubTool(drifted, counter)  # noqa: SLF001

    result = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
    )

    assert result.status is ToolStatus.DENIED, result
    assert result.error is not None
    assert result.error.details["channel"] == "target_objects", result.error.details
    assert result.error.details.get("reason") == "EXTRACTED_TARGETS_EMPTY"
    assert counter.get("calls", 0) == 0
    assert _tool_call_count(control_client, task.task_id) == 0


# ===========================================================================
# T5｜审计只追加：dg_ctl 的四条负测 + 一条正向（approvals 可更新）
# ===========================================================================


def test_t5_dg_ctl_cannot_update_or_delete_audit_history(control_client) -> None:
    """P0-12 / T5 断言 ①②：`task_events` 的 UPDATE、DELETE 必须被数据库拒绝。"""
    with pytest.raises(DatabasePermissionError) as update_error:
        control_client.execute(
            "UPDATE dg_system.task_events SET payload_json = '{\"x\":1}' WHERE 1 = 0"
        )
    assert update_error.value.details.get("mysql_error_code") == 1142

    with pytest.raises(DatabasePermissionError) as delete_error:
        control_client.execute("DELETE FROM dg_system.task_events WHERE 1 = 0")
    assert delete_error.value.details.get("mysql_error_code") == 1142


def test_t5_dg_ctl_cannot_delete_approvals(control_client) -> None:
    """断言 ②：`approvals` 是状态表，可 UPDATE 但**不可 DELETE**。"""
    with pytest.raises(DatabasePermissionError) as delete_error:
        control_client.execute("DELETE FROM dg_system.approvals WHERE 1 = 0")
    assert delete_error.value.details.get("mysql_error_code") == 1142


def test_t5_dg_ctl_cannot_drop_audit_table_and_table_survives(control_client) -> None:
    """断言 ③：直接对**审计表本身**执行 `DROP TABLE`，必须被拒且表仍在。

    工程师原用例用临时表探测（`w06_drop_probe_tmp`）；这里按要求打真表。
    DROP 被授权层拒绝时不会产生任何 DDL，因此对审计表无风险。
    """
    with pytest.raises(DatabasePermissionError) as drop_error:
        control_client.execute("DROP TABLE dg_system.task_events")
    assert drop_error.value.details.get("mysql_error_code") == 1142

    rows = control_client.fetch_all(
        "SELECT COUNT(*) AS total FROM information_schema.tables "
        "WHERE table_schema = 'dg_system' AND table_name = 'task_events'"
    )
    assert int(rows[0]["total"]) == 1, "DROP 失败后审计表必须仍然存在"
    # 表不仅存在，而且仍可读（链本身没被破坏）
    assert control_client.fetch_all("SELECT seq FROM dg_system.task_events LIMIT 1") is not None


def test_t5_dg_ctl_can_still_update_approval_status(control_client) -> None:
    """断言 ④：`approvals` 的 UPDATE 通路未被误伤（状态迁移依赖它）。"""
    affected = control_client.execute(
        "UPDATE dg_system.approvals SET status = status WHERE 1 = 0"
    )
    assert affected == 0


def test_t5_live_grants_show_no_drop_and_no_audit_write(control_client) -> None:
    """不看授权脚本，直接读数据库里的**实际授权**（脚本与线上可能漂移）。"""
    rows = control_client.fetch_all("SHOW GRANTS")
    grants = " ".join(str(list(row.values())[0]) for row in rows).upper()

    # dg_ctl 的 DDL 授权必须不含 DROP
    assert "DROP" not in grants, grants
    # 表级授权里不允许出现对审计表的 UPDATE/DELETE
    assert "UPDATE ON `DG_SYSTEM`.`TASK_EVENTS`" not in grants, grants
    assert "DELETE ON `DG_SYSTEM`.`TASK_EVENTS`" not in grants, grants
    # 审批状态表必须保留 UPDATE
    assert "UPDATE ON `DG_SYSTEM`.`APPROVALS`" in grants, grants


# ===========================================================================
# T7｜审计写入失败 → R2 fail closed（工具未被调用）
# ===========================================================================


def test_t7_step_prepared_audit_failure_blocks_execution(
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
    caplog,
) -> None:
    """让 `STEP_PREPARED` 这一条事件写不进去（其余事件正常），验证 8c 门禁是真阻断点。"""
    task = service.create_task("T7 审计失败阻断")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    worker = worker_factory()
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    original = TaskRepository.insert_event

    def failing_insert(self, event, **kwargs):  # noqa: ANN001, ANN003
        if event.event_type == "STEP_PREPARED":
            raise DatabaseQueryError(
                "模拟审计账本不可写", details={"mysql_error_code": 1045}
            )
        return original(self, event, **kwargs)

    monkeypatch.setattr(TaskRepository, "insert_event", failing_insert)

    with pytest.raises(AuditUnavailableError):
        gateway.execute(
            _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
        )

    assert counter.get("calls", 0) == 0, "审计不可用时工具绝不允许被调用"
    assert _tool_call_count(control_client, task.task_id) == 0
    # 告警：结构化日志必须带 alert 标记
    assert any(
        getattr(record, "alert", None) == "AUDIT_WRITE_FAILED" for record in caplog.records
    ), [record.__dict__.get("alert") for record in caplog.records]


# ===========================================================================
# T8｜结果未知 → NEEDS_RECONCILIATION，且无自动重试
# ===========================================================================


def test_t8_write_timeout_converges_to_needs_reconciliation_without_retry(
    service,
    gateway,
    worker_factory,
    approvals,
    operator,
    approver,
    settings,
    control_client,
    audit,
    step_records,
    counter,
) -> None:
    """走 Gateway 的真实写路径制造超时（不是直接调 executor.run_tool）。"""
    task = service.create_task("T8 写超时")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    task = service.transition(task.task_id, TaskStatus.PLANNING)
    task = service.transition(task.task_id, TaskStatus.EXECUTING)

    # 换成"慢工具"：超时阈值 200ms，工具耗时 600ms
    slow_definition = _definition(timeout_ms=200)
    gateway._registry._tools[TOOL_NAME] = StubTool(  # noqa: SLF001
        slow_definition, counter, delay_s=0.6
    )

    worker = worker_factory()
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    result = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
    )

    assert result.status is ToolStatus.UNKNOWN_OUTCOME, result
    assert "write_timeout_outcome_unknown" in result.warnings
    assert counter["calls"] == 1, "写操作绝不允许自动重试"

    events = _event_types(audit, task.task_id)
    assert "TOOL_CALL_RETRIED" not in events, events
    assert _tool_call_count(control_client, task.task_id) == 1

    # 执行链收敛：Reconciler 依据"已 Prepared 未 Observed"给出确定结论且不重放
    reconciler = Reconciler(step_records, service.repository, audit)
    results = reconciler.sweep(tenant_id=task.tenant_id, older_than_s=0)
    mine = [item for item in results if item.task_id == task.task_id]
    assert mine, "sweep 必须发现本任务未观测的写调用"
    assert {item.conclusion for item in mine} <= {"NEEDS_RECONCILIATION", "MANUAL_INTERVENTION"}
    assert service.get_task(task.task_id).status is TaskStatus.NEEDS_RECONCILIATION
    assert counter["calls"] == 1, "对账不得重放工具"


# ===========================================================================
# P0-16｜写操作成功后必须留下 StepObserved 证据（否则对账必然误判）
# ===========================================================================


def test_p016_successful_write_leaves_observed_evidence(
    service,
    gateway,
    worker_factory,
    approvals,
    operator,
    approver,
    settings,
    audit,
    step_records,
    counter,
) -> None:
    """写操作执行后必须持久化 `StepObserved`（P0-16 后半句）。

    这条断言的价值不只是"表里有行"：`Reconciler.find_unobserved()` 以
    "`step_prepared` 存在但 `step_observed` 缺失"为待核对条件。若成功路径不写
    `step_observed`，一次**成功**的写入会被对账流程误判为"需要核对"，
    进而把 `EXECUTING` 任务错误迁移到 `NEEDS_RECONCILIATION`。
    """
    task = service.create_task("P0-16 成功写入的观测证据")
    detail = _approved(approvals, service.get_task(task.task_id), operator, approver)
    task = service.transition(task.task_id, TaskStatus.PLANNING)
    task = service.transition(task.task_id, TaskStatus.EXECUTING)

    worker = worker_factory()
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    result = gateway.execute(
        _envelope(settings, lease, task, approval_id=detail.approval_id), dict(PARAMS)
    )
    assert result.status is ToolStatus.SUCCESS, result.error
    assert counter["calls"] == 1

    prepared_rows = step_records.find_unobserved(tenant_id=task.tenant_id, older_than_s=0)
    prepared_ids = {row.tool_call_id for row in prepared_rows}
    assert step_records.get_observed(result.tool_call_id) is not None, (
        "写操作执行后必须写入 step_observed（P0-16）；"
        f"当前 {result.tool_call_id} 在 unobserved 集合中={result.tool_call_id in prepared_ids}"
    )

    reconciler = Reconciler(step_records, service.repository, audit)
    reconciler.sweep(tenant_id=task.tenant_id, older_than_s=0)
    assert service.get_task(task.task_id).status is TaskStatus.EXECUTING, (
        "已成功的写操作不得被对账流程迁移到 NEEDS_RECONCILIATION"
    )


# ===========================================================================
# API 侧：T1 字段完整性 / 客户端不可提交 target_objects / 权限矩阵 / legacy 旁路
# ===========================================================================


class _ApiR2Tool:
    def __init__(self) -> None:
        self.definition = _definition()

    def run(self, context: Any, params: Any = None) -> Any:  # pragma: no cover
        raise AssertionError("API 用例不执行工具")


@pytest.fixture
def api_env(monkeypatch, control_client) -> Iterator[SimpleNamespace]:
    """真实 FastAPI 应用 + 真实控制库；身份用 `dependency_overrides` 注入。

    只替换 `get_principal`（认证本身由 W05 用例覆盖），其余依赖（控制库、审批服务、
    审计服务、路由、错误处理）全部走生产路径。

    租户复用控制库里既有的 `tenant_local`/`env_mysql_local`（不再新建租户），
    跨租户用例使用一个**不存在**的租户 id —— 这与"另一租户的身份访问本租户审批"
    在服务端的效果等价（都只能返回"找不到"），且不需要管理员账号清理。
    """
    registry = ToolRegistry()
    registry.register(_ApiR2Tool())
    monkeypatch.setattr("apps.api.deps.build_observer_registry", lambda: registry)
    monkeypatch.setattr("apps.api.routes.tasks.build_observer_registry", lambda: registry)

    suffix = uuid4().hex[:8]
    tenant_id = "tenant_local"
    environment_id = "env_mysql_local"
    other_tenant_id = f"qa_absent_tenant_{suffix}"

    application = create_app()
    holder: dict[str, Principal] = {}

    def _principal() -> Principal:
        return holder["current"]

    application.dependency_overrides[security_get_principal] = _principal
    task_ids: list[str] = []

    def make_task(label: str = "main") -> str:
        task = TaskService(TaskRepository(control_client), ToolRegistry()).create_task(
            f"QA API 用例 {label}",
            tenant_id=tenant_id,
            environment_id=environment_id,
            created_by="user_qa_operator",
        )
        task_ids.append(task.task_id)
        return task.task_id

    def as_role(role: Role, tenant: str | None = None) -> None:
        holder["current"] = Principal(
            user_id=f"qa_{role.value.lower()}_{suffix}",
            tenant_id=tenant or tenant_id,
            roles=(role,),
            session_id=f"sess_qa_{role.value.lower()}",
        )

    try:
        with TestClient(application) as client:
            yield SimpleNamespace(
                client=client,
                holder=holder,
                as_role=as_role,
                make_task=make_task,
                tenant_id=tenant_id,
                other_tenant_id=other_tenant_id,
                environment_id=environment_id,
            )
    finally:
        application.dependency_overrides.clear()
        for task_id in task_ids:
            _cleanup_task_rows(control_client, task_id)


def _create_body(task_id: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "task_id": task_id,
        "tool_name": TOOL_NAME,
        "params": dict(PARAMS),
        "operator_note": "QA 验收",
    }
    body.update(extra)
    return body


def test_t1_detail_returns_every_prd_section4_field(api_env, control_client) -> None:
    """T1：详情必须返回 PRD §4 全部字段，且 `action_digest` 可被独立复算。"""
    from core.security.canonical import canonical_digest

    task_id = api_env.make_task()
    api_env.as_role(Role.OPERATOR)
    created = api_env.client.post("/v1/approvals", json=_create_body(task_id))
    assert created.status_code == 200, created.text
    approval_id = created.json()["approval_id"]

    api_env.as_role(Role.APPROVER)
    detail = api_env.client.get(f"/v1/approvals/{approval_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()

    # 绑定信息：五件套齐全
    assert body["action_digest"] and len(body["action_digest"]) == 64
    assert body["environment_fingerprint"]
    assert body["policy_version"]
    assert body["db_engine"] == "mysql"
    assert body["environment_id"] == api_env.environment_id
    assert body["tenant_id"] == api_env.tenant_id

    # 独立复算：详情里的展示全文必须能复算出登记摘要（同源）
    assert canonical_digest(body["action_descriptor"]) == body["action_digest"]

    disclosure = body["disclosure"]
    expected_sections = {
        "操作",
        "目标对象",
        "影响面",
        "风险等级",
        "前置条件",
        "补偿方案",
        "验证计划",
        "有效期",
        "绑定信息",
    }
    assert expected_sections <= set(disclosure), sorted(disclosure)

    # 每一节都必须显式声明 provided；不可用字段不得折叠成"看起来有"
    for section in expected_sections:
        assert "provided" in disclosure[section], section
    assert disclosure["操作"]["tool_name"] == TOOL_NAME
    assert disclosure["操作"]["tool_version"] == "1.0"
    assert disclosure["目标对象"]["items"], "目标对象不得只给数量"
    assert disclosure["目标对象"]["object_count"] == len(disclosure["目标对象"]["items"])
    assert disclosure["影响面"]["side_effect"] == "write"
    assert disclosure["前置条件"]["digest"] == body["prerequisites_digest"]
    # 序列化口径可能不同（`Z` vs `+00:00`），比较的是同一个时刻而不是字符串
    assert datetime.fromisoformat(
        disclosure["有效期"]["expires_at"].replace("Z", "+00:00")
    ) == datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert disclosure["绑定信息"]["action_digest"] == body["action_digest"]
    assert body["missing_required_fields"] == []


def test_t1_missing_required_field_blocks_approval(api_env, control_client) -> None:
    """PRD §4 硬规则①：必需字段缺失 → 阻止批准（API 层可测）。"""
    task_id = api_env.make_task()
    api_env.as_role(Role.OPERATOR)
    approval_id = api_env.client.post("/v1/approvals", json=_create_body(task_id)).json()[
        "approval_id"
    ]

    # 把"前置条件全文"清空（模拟记录残缺）；`approvals` 对 dg_ctl 有 UPDATE 权限
    control_client.execute(
        "UPDATE dg_system.approvals SET prerequisites_json = JSON_ARRAY() WHERE approval_id = %s",
        (approval_id,),
    )

    api_env.as_role(Role.APPROVER)
    detail = api_env.client.get(f"/v1/approvals/{approval_id}").json()
    assert "prerequisites" in detail["missing_required_fields"], detail["missing_required_fields"]

    denied = api_env.client.post(f"/v1/approvals/{approval_id}/approve", json={})
    assert denied.status_code == 409, denied.text
    assert "prerequisites" in str(denied.json()["error"]["details"])
    rows = control_client.fetch_all(
        "SELECT status FROM dg_system.approvals WHERE approval_id = %s", (approval_id,)
    )
    assert rows[0]["status"] == "PENDING"


def test_p03b_client_supplied_target_objects_cannot_override_extraction(
    api_env, control_client
) -> None:
    """P0-03b：目标对象由服务端从参数抽取，客户端**不可提交**。

    这里提交一份与 `params` 矛盾的目标对象（`orders_archive`），
    断言审批绑定的是"参数里真正会被操作的对象"（`orders`）。
    若实现信任客户端提交值，审批人看到的对象与将要执行的对象就不是同一份数据
    （违反 PRD §4 硬规则②），必须判为不通过。
    """
    task_id = api_env.make_task()
    api_env.as_role(Role.OPERATOR)
    forged = [
        {
            "object_type": "row",
            "database": "dg_lab",
            "schema": None,
            "name": "orders_archive",
            "keys": ["id=9999"],
        }
    ]
    response = api_env.client.post(
        "/v1/approvals", json=_create_body(task_id, target_objects=forged)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    names = {item["name"] for item in body["disclosure"]["目标对象"]["items"]}
    assert names == {"orders"}, (
        f"审批披露的目标对象必须由服务端从 params 抽取；实际={names}"
    )
    assert body["disclosure"]["操作"]["action_descriptor"]["parameters"]["table"] == "orders"


def test_permission_matrix_and_cross_tenant_404(api_env, control_client) -> None:
    """权限矩阵：Approver 可裁决、Administrator 裁决 FORBIDDEN、Auditor 只读审计、
    跨租户访问返回 404（不是 403，避免泄漏存在性）。"""
    from core.errors import ErrorCode

    task_id = api_env.make_task()
    api_env.as_role(Role.OPERATOR)
    created = api_env.client.post("/v1/approvals", json=_create_body(task_id))
    approval_id = created.json()["approval_id"]

    # Administrator 裁决 → FORBIDDEN（职责分离，不得为此新增权限）
    api_env.as_role(Role.ADMINISTRATOR)
    admin_approve = api_env.client.post(f"/v1/approvals/{approval_id}/approve", json={})
    assert admin_approve.status_code == 403, admin_approve.text
    assert admin_approve.json()["error"]["error_code"] == ErrorCode.FORBIDDEN.value

    # Auditor：能读审计链，不能读审批详情
    api_env.as_role(Role.AUDITOR)
    events = api_env.client.get(f"/v1/audit/events?task_id={task_id}")
    assert events.status_code == 200, events.text
    assert any(
        item["payload"].get("approval_id") == approval_id for item in events.json()["items"]
    )
    auditor_detail = api_env.client.get(f"/v1/approvals/{approval_id}")
    assert auditor_detail.status_code == 403

    # 跨租户（另一租户的 Approver）→ 404 而不是 403
    api_env.as_role(Role.APPROVER, tenant=api_env.other_tenant_id)
    cross = api_env.client.get(f"/v1/approvals/{approval_id}")
    assert cross.status_code == 404, cross.text
    assert cross.json()["error"]["error_code"] == ErrorCode.APPROVAL_NOT_FOUND.value

    # Approver 正常裁决 → 200
    api_env.as_role(Role.APPROVER)
    approved = api_env.client.post(f"/v1/approvals/{approval_id}/approve", json={})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"


def test_legacy_tool_call_route_cannot_execute_r2(api_env, control_client) -> None:
    """legacy `POST /tasks/{id}/tools/{name}` 对 R2 必须仍然拒绝，且不提供审批入口。

    注：仓库里这条 legacy 路由没有 `/v1` 前缀（`apps/api/routes/tasks.py:29`
    `APIRouter(prefix="/tasks")`），因此这里按**真实路径**测试；
    `/v1/tasks/{id}/tools/{name}` 本身不存在（404），同样不构成旁路。
    """
    task_id = api_env.make_task()
    api_env.as_role(Role.OPERATOR)

    response = api_env.client.post(
        f"/tasks/{task_id}/tools/{TOOL_NAME}", json={"params": dict(PARAMS)}
    )
    assert response.status_code == 403, response.text
    body = response.json()
    assert "approval_id" not in str(body), f"legacy 路由不得返回 approval_id：{body}"
    assert body["error"]["error_code"] == "POLICY_DENIED"
    assert body["error"]["details"]["deny_code"] == "APPROVAL_REQUIRED"
    assert body["error"]["details"]["required_approval"] is True
    assert _tool_call_count(control_client, task_id) == 0

    # `/v1` 变体不存在 → 不能作为绕过入口
    v1_response = api_env.client.post(
        f"/v1/tasks/{task_id}/tools/{TOOL_NAME}", json={"params": dict(PARAMS)}
    )
    assert v1_response.status_code == 404
    assert _tool_call_count(control_client, task_id) == 0


# ===========================================================================
# T6｜完整性校验可定位篡改（应用层可验部分）
# ===========================================================================


def test_t6_in_memory_tamper_is_located_precisely(service, audit, control_client) -> None:
    """T6 的应用层部分：篡改任意一条事件后，校验必须给出**精确**断裂位置。

    真正的"用高权账号改库内行"需要管理员账号（当前环境不可用，见测试报告），
    这里改为在**读出的链副本**上做篡改：校验函数拿到的输入与"库内行被改过"后的
    输入完全等价（`verify_event_chain` 是纯函数，不知道数据来自哪）。
    """
    task = service.create_task("T6 篡改定位（内存副本）")
    for index in range(3):
        audit.emit_required(
            make_event(
                AuditEventType.AUDIT_INTEGRITY_FAILED,
                task_id=task.task_id,
                run_id=task.run_id,
                payload={"index": index},
            ),
            actor_user_id="user_qa",
        )

    chain = audit._load_chain(task.task_id)  # noqa: SLF001 - 只读，与 verify 同一入口
    assert verify_event_chain(chain).ok is True

    target = chain[2]
    tampered = list(chain)
    tampered[2] = target.model_copy(update={"payload": {"tampered": True}})
    result = verify_event_chain(tampered)

    assert result.ok is False
    assert result.broken_at_seq == target.seq
    assert result.affected_range is not None
    assert result.affected_range[1] == chain[-1].seq

    # 删除中间一条 → 必须报"序号断档"而不是静默通过
    deleted = [event for index, event in enumerate(chain) if index != 2]
    gap = verify_event_chain(deleted)
    assert gap.ok is False
    assert gap.broken_at_seq == target.seq


# ===========================================================================
# T11｜不得宣称 Exactly Once / WORM 已具备
# ===========================================================================


_NEGATIONS = (
    "没有",
    "不",
    "无",
    "未",
    "禁",
    "否",
    "非",
    "not",
    "never",
    "no ",
    "without",
)


def test_t11_docs_do_not_claim_exactly_once_or_worm() -> None:
    """T11：全仓文档不得出现"Exactly Once / 精确一次 / WORM 已具备"的肯定性宣称。

    判定口径：命中关键词后检查"上一行 + 本行 + 下一行"是否含否定词
    （中文语境下判断常在同行后半句或紧邻行给出）；无否定词即视为肯定性宣称。
    """
    root = Path(__file__).resolve().parents[2]
    patterns = ("Exactly Once", "exactly once", "精确一次", "WORM 已具备", "WORM已具备")
    offenders: list[str] = []

    candidates: list[Path] = []
    for relative in ("docs", "README.md", "core", "apps"):
        path = root / relative
        if path.is_dir():
            candidates.extend(sorted(path.rglob("*.md")))
        elif path.is_file():
            candidates.append(path)

    for path in candidates:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            if not any(token in line for token in patterns):
                continue
            window = "\n".join(lines[max(0, index - 1) : index + 2])
            if not any(neg in window for neg in _NEGATIONS):
                offenders.append(f"{path.relative_to(root)}:{index + 1}: {line.strip()[:160]}")

    assert not offenders, "存在肯定性宣称：\n" + "\n".join(offenders)


def test_t11_no_exactly_once_in_code_comments_only() -> None:
    """代码注释里同样不得出现肯定性宣称（用 grep 级粗筛，人工复核见测试报告）。"""
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "grep", "-n", "-I", "-E", "Exactly Once|精确一次"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    hits = [line for line in result.stdout.splitlines() if line.strip()]
    offenders = [
        line
        for line in hits
        if not any(neg in line.split(":", 2)[-1] for neg in _NEGATIONS)
    ]
    assert not offenders, "存在肯定性宣称：\n" + "\n".join(offenders)
