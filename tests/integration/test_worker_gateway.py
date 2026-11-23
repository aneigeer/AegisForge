"""W05 集成测试：Worker 租约/信封 → 唯一 Tool Gateway → 审计与执行。

依赖真实 MySQL（控制库）与已应用迁移 005。
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from core.errors import (
    DatabaseQueryError,
    EnvelopeInvalidError,
    FencingTokenStaleError,
    NonceReplayError,
    ToolNotFoundError,
    WorkerConflictError,
    WorkerLeaseInvalidError,
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


# ---------------------------------------------------------------------------
# 测试桩工具与夹具
# ---------------------------------------------------------------------------


def _definition(
    name: str,
    *,
    risk_level: str = "R0",
    side_effect: str = "read",
    permissions: list[str] | None = None,
    version: str = "1.0",
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="W05 Gateway 测试桩工具",
        version=version,
        risk_level=risk_level,  # type: ignore[arg-type]
        side_effect=side_effect,  # type: ignore[arg-type]
        permissions=list(permissions or ["mysql:read"]),
        timeout_ms=30_000,
        idempotent=True,
        audit_policy="metadata",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        db_engines=["mysql"],
        allowed_environments=["LAB"],
        operation_class="observe",
        # W07：写工具还需声明补偿方案与验证计划（构造期 fail closed）。
        **(write_declarations() if side_effect == "write" else {}),
    )


class StubTool:
    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition
        self.input_model = None

    def run(self, context: ToolContext, params: dict[str, object] | None = None) -> ToolResult:
        return ToolResult(
            tool_name=self.definition.name,
            status=ToolStatus.SUCCESS,
            task_id=context.task_id,
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            started_at=utc_now_iso(),
            duration_ms=0.0,
            data={"ok": True, "params": params or {}},
            warnings=[],
            error=None,
            source=ToolSource(
                database_id="stub",
                db_version="8.0",
                collected_at=utc_now_iso(),
            ),
        )


@pytest.fixture(autouse=True)
def _cleanup_created_tasks(admin_client, monkeypatch):
    """清理本用例创建的任务及其租约/信封/事件/工具调用。"""
    created: list[str] = []
    original = TaskService.create_task

    def tracked(self, goal, **kwargs):
        task = original(self, goal, **kwargs)
        created.append(task.task_id)
        return task

    monkeypatch.setattr(TaskService, "create_task", tracked)
    yield
    for task_id in created:
        try:
            admin_client.execute(
                "DELETE FROM dg_system.evidence WHERE task_id = %s", (task_id,)
            )
        except DatabaseQueryError:
            pass
        for table in (
            "task_events",
            "tool_calls",
            "task_steps",
            "checkpoints",
            "execution_envelopes",
            "worker_leases",
            "tasks",
        ):
            admin_client.execute(
                f"DELETE FROM dg_system.{table} WHERE task_id = %s", (task_id,)
            )


@pytest.fixture
def repository(control_client) -> TaskRepository:
    return TaskRepository(control_client)


@pytest.fixture
def registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(StubTool(_definition("stub_read")))
    registry.register(StubTool(_definition("stub_r3", risk_level="R3")))
    registry.register(StubTool(_definition("stub_r4", risk_level="R4")))
    return registry


@pytest.fixture
def service(repository: TaskRepository, registry: ToolRegistry) -> TaskService:
    return TaskService(repository, registry)


@pytest.fixture
def lease_service(control_client) -> LeaseService:
    return LeaseService(LeaseRepository(control_client))


@pytest.fixture
def policy_engine(settings) -> PolicyEngine:
    return PolicyEngine(policy_version=settings.policy_version)


@pytest.fixture
def gateway(
    settings,
    repository: TaskRepository,
    registry: ToolRegistry,
    lease_service: LeaseService,
    policy_engine: PolicyEngine,
    ro_client,
) -> ToolGateway:
    executor = ToolExecutor(registry, repository)

    def principal_resolver(user_id: str) -> Principal:
        return Principal(
            user_id=user_id,
            tenant_id="tenant_local",
            roles=(Role.OPERATOR,),
            auth_method=AuthMethod.DEV_LOCAL,
        )

    def context_factory(envelope: ExecutionEnvelope, task: Task) -> ToolContext:
        return ToolContext(client=ro_client, task_id=task.task_id, run_id=task.run_id)

    return ToolGateway(
        registry,
        repository,
        lease_service,
        policy_engine,
        executor,
        principal_resolver,
        context_factory,
        settings=settings,
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


def _signed(
    settings,
    lease: WorkerLease,
    task: Task,
    *,
    tool_name: str = "stub_read",
    params: dict[str, object] | None = None,
    tool_version: str = "1.0",
    fencing_token: int | None = None,
    worker_id: str | None = None,
    lease_id: str | None = None,
    ttl_s: float = 60,
    now=None,
    model_risk_hint: str | None = None,
) -> ExecutionEnvelope:
    envelope = build_envelope(
        task_id=task.task_id,
        step_id=None,
        worker_id=worker_id or lease.worker_id,
        lease_id=lease_id or lease.lease_id,
        fencing_token=lease.fencing_token if fencing_token is None else fencing_token,
        state_version=task.state_version,
        correlation_id="corr_integration",
        actor_user_id="user_local",
        tool_name=tool_name,
        tool_version=tool_version,
        environment_id=task.environment_id,
        params=params if params is not None else {"limit": 1},
        ttl_s=ttl_s,
        model_risk_hint=model_risk_hint,  # type: ignore[arg-type]
        now=now,
    )
    return sign_envelope(envelope, settings.gateway_key())


def _tool_call_count(client, task_id: str) -> int:
    rows = client.fetch_all(
        "SELECT COUNT(*) AS total FROM dg_system.tool_calls WHERE task_id = %s",
        (task_id,),
    )
    return int(rows[0]["total"]) if rows else 0


def _event_types(repository: TaskRepository, task_id: str) -> set[str]:
    return {event.event_type for event in repository.list_events(task_id)}


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_readonly_tool_executes_and_writes_audit_chain(
    service: TaskService,
    repository: TaskRepository,
    worker_factory,
    control_client,
) -> None:
    task = service.create_task("W05 正常只读执行")
    worker = worker_factory("worker_a")
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    assert lease.fencing_token == 1

    result = worker.submit(
        "stub_read",
        {"limit": 1},
        step_id=None,
        state_version=task.state_version,
    )

    assert result.status == ToolStatus.SUCCESS
    assert result.task_id == task.task_id
    assert result.tool_call_id

    record = repository.get_tool_call(result.tool_call_id)
    assert record is not None
    assert record.task_id == task.task_id
    assert record.status == "success"

    events = _event_types(repository, task.task_id)
    assert "GATEWAY_EXECUTION_ALLOWED" in events
    assert "TOOL_CALL_SUCCEEDED" in events

    rows = control_client.fetch_all(
        "SELECT COUNT(*) AS total FROM dg_system.execution_envelopes WHERE task_id = %s",
        (task.task_id,),
    )
    assert int(rows[0]["total"]) == 1


# ---------------------------------------------------------------------------
# 重放与租约/fencing 校验
# ---------------------------------------------------------------------------


def test_nonce_replay_is_rejected(
    service: TaskService,
    gateway: ToolGateway,
    settings,
    worker_factory,
) -> None:
    task = service.create_task("W05 nonce 重放")
    worker = worker_factory("worker_a")
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    envelope = _signed(settings, lease, task)

    first = gateway.execute(envelope, {"limit": 1})
    assert first.status == ToolStatus.SUCCESS

    with pytest.raises(NonceReplayError) as excinfo:
        gateway.execute(envelope, {"limit": 1})
    assert excinfo.value.code.value == "NONCE_REPLAY"


def test_old_lease_after_release_is_rejected(
    service: TaskService,
    gateway: ToolGateway,
    lease_service: LeaseService,
    settings,
    worker_factory,
) -> None:
    task = service.create_task("W05 旧租约")
    worker_a = worker_factory("worker_a")
    lease_a = worker_a.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    envelope_a = _signed(settings, lease_a, task)

    lease_service.release(lease_a.lease_id, "worker_a", lease_a.fencing_token)

    worker_b = worker_factory("worker_b")
    lease_b = worker_b.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    assert lease_b.fencing_token > lease_a.fencing_token

    with pytest.raises(WorkerLeaseInvalidError):
        gateway.execute(envelope_a, {"limit": 1})


def test_stale_fencing_token_is_rejected(
    service: TaskService,
    gateway: ToolGateway,
    lease_service: LeaseService,
    settings,
    worker_factory,
) -> None:
    task = service.create_task("W05 旧 fencing token")
    worker_a = worker_factory("worker_a")
    lease_a = worker_a.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    lease_service.release(lease_a.lease_id, "worker_a", lease_a.fencing_token)

    worker_b = worker_factory("worker_b")
    lease_b = worker_b.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    envelope = _signed(settings, lease_b, task, fencing_token=lease_a.fencing_token)

    with pytest.raises(FencingTokenStaleError) as excinfo:
        gateway.execute(envelope, {"limit": 1})
    assert excinfo.value.details["current_max_token"] == lease_b.fencing_token


def test_second_worker_conflicts_while_lease_active(
    service: TaskService,
    worker_factory,
) -> None:
    task = service.create_task("W05 双 Worker 冲突")
    worker_a = worker_factory("worker_a")
    lease_a = worker_a.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    worker_b = worker_factory("worker_b")
    with pytest.raises(WorkerConflictError) as excinfo:
        worker_b.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    assert excinfo.value.details["lease_id"] == lease_a.lease_id
    assert excinfo.value.details["worker_id"] == "worker_a"


def test_expired_lease_blocks_heartbeat_and_new_worker_gets_bigger_token(
    service: TaskService,
    admin_client,
    lease_service: LeaseService,
    worker_factory,
) -> None:
    task = service.create_task("W05 过期租约")
    worker_a = worker_factory("worker_a")
    lease_a = worker_a.acquire_lease(task.task_id, task.environment_id, ttl_s=30)

    admin_client.execute(
        "UPDATE dg_system.worker_leases "
        "SET expires_at = DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 10 SECOND) "
        "WHERE lease_id = %s",
        (lease_a.lease_id,),
    )

    with pytest.raises(WorkerLeaseInvalidError):
        worker_a.heartbeat(30)
    assert worker_a.lease is None
    with pytest.raises(WorkerLeaseInvalidError):
        worker_a.submit(
            "stub_read", {"limit": 1}, step_id=None, state_version=task.state_version
        )
    with pytest.raises(WorkerLeaseInvalidError):
        lease_service.verify(
            lease_a.lease_id, task.task_id, "worker_a", lease_a.fencing_token
        )

    worker_b = worker_factory("worker_b")
    lease_b = worker_b.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    assert lease_b.fencing_token > lease_a.fencing_token


# ---------------------------------------------------------------------------
# 信封内容与策略拒绝
# ---------------------------------------------------------------------------


def test_expired_envelope_is_rejected(
    service: TaskService,
    gateway: ToolGateway,
    settings,
    worker_factory,
) -> None:
    task = service.create_task("W05 过期信封")
    worker = worker_factory("worker_a")
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    base = utc_now()
    envelope = _signed(settings, lease, task, ttl_s=30, now=base)

    with pytest.raises(EnvelopeInvalidError):
        gateway.execute(
            envelope, {"limit": 1}, now=base + timedelta(seconds=31)
        )


def test_tampered_params_are_rejected(
    service: TaskService,
    gateway: ToolGateway,
    settings,
    worker_factory,
) -> None:
    task = service.create_task("W05 参数篡改")
    worker = worker_factory("worker_a")
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    envelope = _signed(settings, lease, task, params={"limit": 1})

    with pytest.raises(EnvelopeInvalidError) as excinfo:
        gateway.execute(envelope, {"limit": 2})
    assert "摘要" in excinfo.value.message


def test_unregistered_tool_is_rejected(
    service: TaskService,
    gateway: ToolGateway,
    settings,
    worker_factory,
) -> None:
    task = service.create_task("W05 未注册工具")
    worker = worker_factory("worker_a")
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    envelope = _signed(settings, lease, task, tool_name="unregistered_tool")

    with pytest.raises(ToolNotFoundError):
        gateway.execute(envelope, {"limit": 1})


def test_forged_higher_fencing_token_is_rejected(
    service: TaskService,
    gateway: ToolGateway,
    settings,
    worker_factory,
) -> None:
    task = service.create_task("W05 伪造更高 fencing token")
    worker = worker_factory("worker_a")
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    envelope = _signed(
        settings, lease, task, fencing_token=lease.fencing_token + 100
    )

    with pytest.raises(FencingTokenStaleError) as excinfo:
        gateway.execute(envelope, {"limit": 1})
    assert excinfo.value.details["lease_fencing_token"] == lease.fencing_token


def test_prod_environment_is_denied_before_execution(
    service: TaskService,
    repository: TaskRepository,
    gateway: ToolGateway,
    settings,
    worker_factory,
    admin_client,
    control_client,
) -> None:
    env_id = f"env_w05_prod_{uuid4().hex[:8]}"
    admin_client.execute(
        "INSERT INTO dg_system.environments (environment_id, tenant_id, db_engine, "
        "db_version, network_id, image_digest, config_version, status, created_at) "
        "VALUES (%s, 'tenant_local', 'mysql', '8.0.46', 'lab_net', NULL, "
        "'w05-test', 'PROD', UTC_TIMESTAMP(6))",
        (env_id,),
    )
    task = service.create_task("W05 PROD 目标拒绝", environment_id=env_id)
    try:
        worker = worker_factory("worker_a")
        worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
        result = worker.submit(
            "stub_read",
            {"limit": 1},
            step_id=None,
            state_version=task.state_version,
        )

        assert result.status == ToolStatus.DENIED
        assert result.error is not None
        assert result.error.code == "POLICY_DENIED"
        assert result.error.details["reason"] == "PROD_TARGET_NOT_ALLOWED"
        assert _tool_call_count(control_client, task.task_id) == 0
        assert "GATEWAY_EXECUTION_DENIED" in _event_types(repository, task.task_id)
    finally:
        for table in (
            "task_events",
            "tool_calls",
            "task_steps",
            "checkpoints",
            "execution_envelopes",
            "worker_leases",
            "tasks",
        ):
            admin_client.execute(
                f"DELETE FROM dg_system.{table} WHERE task_id = %s", (task.task_id,)
            )
        admin_client.execute(
            "DELETE FROM dg_system.environments WHERE environment_id = %s", (env_id,)
        )


@pytest.mark.parametrize(
    ("tool_name", "risk"),
    [("stub_r3", "R3"), ("stub_r4", "R4")],
)
def test_high_risk_tools_are_denied_without_tool_calls(
    service: TaskService,
    repository: TaskRepository,
    gateway: ToolGateway,
    settings,
    worker_factory,
    control_client,
    tool_name: str,
    risk: str,
) -> None:
    task = service.create_task(f"W05 高风险拒绝 {risk}")
    worker = worker_factory("worker_a")
    lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    envelope = _signed(settings, lease, task, tool_name=tool_name)

    result = gateway.execute(envelope, {"limit": 1})

    assert result.status == ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "POLICY_DENIED"
    assert result.error.details["effective_risk"] == risk
    assert result.error.details["policy_version"] == settings.policy_version

    assert _tool_call_count(control_client, task.task_id) == 0
    events = _event_types(repository, task.task_id)
    assert "POLICY_DECISION" in events
    assert "GATEWAY_EXECUTION_DENIED" in events
